# Data Model: Шифрованное распределённое хранение токена бота

**Date**: 2026-10-19 | **Spec**: [spec.md](spec.md) | **Plan**: [plan.md](plan.md)

## Сущности

### Bot — расширяется (миграция `0003`)

| Поле | Тип | NULL | Кто пишет в этой фиче |
|---|---|---|---|
| bot_slug | String, PK | нет | генерируется при создании (R7), прежние правила |
| token_half | Text | **да** | табличная зашифрованная половина (hex-блоб, R4/R5) |
| pair_nonce | String(16) | **да** | hex nonce парности, тот же, что в файле (R3) |
| created_at | timestamptz NOT NULL | нет | server_default now() — не трогаем |

NULL-колонки законны: строки, созданные до фичи (без шифрованной пары),
остаются в реестре, но их бот NotReady (живого plaintext-пути больше нет,
FR-014). Живой бот = `token_half IS NOT NULL AND pair_nonce IS NOT NULL`.

### Файл `bots/.env-<bot_slug>` — новый формат

```
GRAMMQ_BOT_NAME=<публичное имя, ≤64 симв., без управляющих>
GRAMMQ_BOT_FILE_HALF=hex( nonce_12 ‖ AESGCM(nonce_12, "<id>:<secret[:4]>", aad) )
GRAMMQ_BOT_PAIR_NONCE=hex( nonce_8 )
```

- plaintext файловой половины до шифрования: `"{числовой_id}:{secret[:4]}"`;
  табличной: `secret[4:]` (шифруется отдельно, тот же aad);
- `aad` = сырые 8 байт nonce парности (R3); запись атомарна (tmp +
  `os.replace`), права 0600, каталог 0700 (`_check_bots_dir` визарда
  переезжает в `token_store.py`);
- старый формат (`GRAMMQ_BOT_TOKEN`) парсером не распознаётся → бот
  NotReady; конвертации нет.

### gmq_messages — только снятие FK

`bot_slug` остаётся `String NOT NULL` (историческая запись журнала), FK
`fk_gmq_messages_bot_slug_gmq_bots` удаляется миграцией `0003`. Проверка
существования бота при enqueue становится явной (`SELECT EXISTS` в
`PostgresBroker.enqueue` → `UnknownBotError`, поведение API 404 прежнее).

### TokenLookup — результат порта `BotConfig.token_for`

| Значение | Условие | Действие runner |
|---|---|---|
| `Ready(token)` | строка есть; обе половины расшифрованы; nonce совпал; формат склейки валиден | отправка |
| `NotRegistered` | строки реестра нет (удалён/неизвестен) | permanent → dead_letter «bot not registered» |
| `NotReady` | строка есть; файла нет / расшифровка или парность провалились / мастер-ключ не задан / колонки NULL | retryable «bot config unreadable (file/keys)» |

### Настройки (Settings, три новых поля)

| Поле | Тип | Дефолт | Секрет | Смысл |
|---|---|---|---|---|
| bot_bootstrap_enabled | bool | `False` | нет | окно управления ботами: оба эндпоинта |
| bot_bootstrap_token | SecretStr \| None | `None` | да | полный токен создаваемого бота (окно) |
| token_master_key | SecretStr \| None | `None` | да | мастер-ключ AESGCM половин (постоянный) |

Переменные: `GRAMMQ_BOT_BOOTSTRAP_ENABLED`, `GRAMMQ_BOT_BOOTSTRAP_TOKEN`,
`GRAMMQ_TOKEN_MASTER_KEY`. Читаются на старте; смена — редеплой/рестарт.

## Миграция `20261019_0003_encrypted_bot_tokens.py`

```text
upgrade:
  add_column gmq_bots token_half TEXT NULL
  add_column gmq_bots pair_nonce VARCHAR(16) NULL
  drop_constraint fk_gmq_messages_bot_slug_gmq_bots ON gmq_messages (FK)
  drop_constraint fk_gmq_bot_claim_state_bot_slug_gmq_bots ON gmq_bot_claim_state (FK)
downgrade: в обратном порядке (перед восстановлением FK требуется, чтобы
  все bot_slug журнала и claim-леджера существовали в gmq_bots —
  документированное ограничение downgrade)
```

Имена объектов — через `table_name()`, префикс фиксирован с 0001.

## Валидации (до любых записей)

- **Токен настройки**: существующий контракт `{id}:{secret}` — ровно один
  `:`, числовой непустой id, непустой секрет без управляющих; секрет
  длиной ≥ 4 для разреза. Нарушение → 503 (единая формулировка с
  «отсутствует», без подсказок).
- **Имя**: непустое, ≤ 64, без управляющих (правила `_validate_name`
  визарда) → 400.
- **Мастер-ключ**: hex, 64 символа → 32 байта; иначе трактуется как
  отсутствующий → 503.
- **Slug**: действующий контракт SLUG_RE `<last4>-<word>-<word>`;
  уникальность — файл + реестр (R7); исчерпание 50 попыток → 500.
- **Взятый токен**: дедуп-скан файловых половин (R8): живой бот с тем же
  числовым id → 409.

## Жизненные циклы

### Создание (`POST /bootstrap/bots`, окно открыто)

```
оператор: BotFather-токен → GRAMMQ_BOT_BOOTSTRAP_TOKEN (панель, редеплой)
   └─ POST /bootstrap/bots {"name": "..."}
        ├─ флаг выкл → 404 (эндпоинт не существует)
        ├─ токен/имя/мастер-ключ невалидны → 503/400, ничего не пишется
        ├─ дедуп-скан: живой бот с этим id → 409
        ├─ скан нашёл «файл без строки» (оборванное создание) →
        │    перезапись обеих половин под прежним slug, дописывание строки
        ├─ генерация slug (last4 id + 2 слова) → шифрование половин с
        │    свежим nonce парности → файл (0600) → строка+claim_state
        └─ 201 {"status":"created","bot_slug":slug}
оператор: флаг → false, токен-сеттинг очистить (редеплой)
```

### Ревок (`DELETE /bootstrap/bots/{slug}`, окно открыто)

```
DELETE → 404 (флаг выкл / slug неизвестен/уже удалён)
       → транзакция: delete gmq_bots (строка claim-леджера gmq_bot_claim_state
         СОЗНАТЕЛЬНО остаётся: claim-ротация итерирует её, и только так
         очередь отозванного бота доходит до воркера)
         → unlink файла половины
       → 200 {"status":"revoked"}
очередь бота: claim (по живой строке леджера) → SplitFileBotConfig →
  NotRegistered → dead_letter «bot not registered» (немедленно, без ретраев)
новые POST /api/v1/messages к slug → 404 bot not found (enqueue-проверка)
```

### Доставка (воркер, каждый claim)

```
token_for(slug):
  строки нет → NotRegistered
  файла нет | колонки NULL | мастер-ключ не задан → NotReady
  decrypt(file_half, aad=nonce) ∥ decrypt(token_half, aad=nonce)
    упало (InvalidTag: подмена/поколения) → NotReady
  validate_token_format(склейка) не прошла → NotReady
  → Ready(токен) — только в памяти, до первого send
```

## Инварианты

- Plaintext токена нет ни в HTTP (запросы/ответы), ни в логах, ни в БД, ни
  в файлах; в покое — только в оконном сеттинге и памяти процесса
  (Principle III v3.0.0, FR-013).
- Ни один носитель в отдельности не даёт рабочего токена даже при известном
  мастер-ключе (разрез R4) — SC-002/SC-003.
- Пара половин одного бота всегда шифруется с одним nonce парности;
  расхождение детектируется до Telegram (AAD-тег).
- Журнал сообщений переживает бота (FK снят); ни одно сообщение не
  исчезает молча — dead-letter с причиной (Principle II).
- bot_claim_state засеивается при создании; строка леджера, как и журнал,
  переживает ревок (историческая запись ротации claim) — именно она
  доводит очередь отозванного бота до dead-letter'а.
