# Tasks: Шифрованное распределённое хранение токена бота

**Input**: Design documents from `/specs/006-encrypted-token-storage/`

**Prerequisites**: plan.md, spec.md, research.md (R1–R13), data-model.md,
contracts/bot-bootstrap-endpoint.md, contracts/token-storage.md

**Tests**: включены — конституция (Engineering Standards / Testing)
обязывает покрывать каждую публичную функцию при покрытии ≥ 80%.

**Organization**: задачи сгруппированы по user story; каждая фаза —
независимо проверяемый инкремент. Формат: `[ID] [P?] [Story] Описание с
путём к файлу`.

## Path Conventions

Single project: `src/`, `tests/`, `alembic/` в корне репозитория
(см. plan.md → Project Structure).

---

## Phase 1: Setup (Shared Infrastructure)

- [x] T001 Добавить зависимость `cryptography` (>=43) в `pyproject.toml`
  (`dependencies`), выполнить `uv sync` (обновить `uv.lock`), проверить
  импорт `cryptography.hazmat.primitives.ciphers.aead.AESGCM` в venv.
  Лицензия Apache-2.0 OR BSD-3-Clause — разрешённый список (research R1).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Цель**: чистое крипто-ядро, slug-модуль, настройки, схема БД и
enqueue-проверка — всё, что нужно каждой истории.

- [x] T002 [P] Создать чистый модуль `src/gram_mq/token_store.py`
  (research R1–R5, contracts/token-storage.md): `validate_token_format`
  (перенос из `cli/token.py`; контракт: ровно один `:`, числовой
  непустой id, непустой секрет без управляющих, секрет длиной ≥ 4);
  `split_token(token) -> (file_plain, db_plain)` — file_plain =
  `"{id}:{secret[:4]}"`, db_plain = `secret[4:]`; `make_pair_nonce()`
  (os.urandom(8)); `parse_master_key(SecretStr) -> bytes | None` (hex 64
  симв. → 32 байта, иначе None); `encrypt_half(key, aad, plain) -> hex`
  и `decrypt_half(key, aad, blob) -> str | None` (AESGCM, nonce 12 байт
  в блобе, InvalidTag → None); `write_bot_file(path, name, file_blob,
  pair_nonce)` — атомарно (tmp + os.replace), 0600, каталог 0700
  (`_check_bots_dir` из `cli/wizard.py` переносится сюда);
  `read_bot_file(path) -> (name, file_blob, pair_nonce) | None` — старый
  формат (`GRAMMQ_BOT_TOKEN`) не распознаётся → None (FR-014).
- [x] T003 [P] Тесты `tests/unit/test_token_store.py`: roundtrip
  encrypt/decrypt; подмена AAD (чужой nonce) → None; подмена блоба
  (перестановка между файлами) → None; split-точка (id и 4 символа /
  остаток); parse_master_key (валидный/короткий/не-hex); запись файла —
  права 0600, атомарность (старое содержимое не теряется при падении),
  чтение старого plaintext-формата → None.
- [x] T004 [P] Перенести генерацию slug из CLI-пакета: `git mv
  src/gram_mq/cli/slug.py src/gram_mq/slug.py` и `git mv
  src/gram_mq/cli/dune_words.py src/gram_mq/dune_words.py`; поправить
  импорты (`cli/wizard.py` — временно, до T025; тесты slug); правила
  генерации не меняются (R7: `<last4>-<word>-<word>`, SystemRandom,
  50 попыток).
- [x] T005 [P] Добавить поля в `src/gram_mq/settings.py` (data-model →
  Настройки): `bot_bootstrap_enabled: bool = False`,
  `bot_bootstrap_token: SecretStr | None = None`,
  `token_master_key: SecretStr | None = None` — все с префиксом
  `GRAMMQ_` через существующий SettingsConfigDict.
- [x] T006 Миграция `alembic/versions/20261019_0003_encrypted_bot_tokens.py`
  (data-model → Миграция): `add_column gmq_bots token_half TEXT NULL`,
  `add_column gmq_bots pair_nonce VARCHAR(16) NULL`,
  `drop_constraint fk_gmq_messages_bot_slug_gmq_bots` (FK, таблица
  gmq_messages); downgrade в обратном порядке с комментарием об
  ограничении (все bot_slug журнала должны существовать в gmq_bots).
  Обновить `src/gram_mq/models/bots.py`: `token_half: Mapped[str | None]`
  (Text), `pair_nonce: Mapped[str | None]` (String(16)).
- [x] T007 Заменить FK-опору enqueue на явную проверку реестра в
  `src/gram_mq/adapters/postgres/broker.py` (research R9): в `enqueue`
  перед INSERT — `SELECT EXISTS(SELECT 1 FROM gmq_bots WHERE bot_slug =
  :slug)`, отсутствие → `UnknownBotError` (контракт и 404 хендлера
  сохраняются); убрать ветку IntegrityError→UnknownBotError. Тест:
  неизвестный slug → UnknownBotError (unit, через fake-сессию или
  integration при GRAMMQ_TEST_DATABASE_URL).
- [x] T008 [P] Создать адаптер реестра
  `src/gram_mq/adapters/postgres/bot_registry.py` (наследник
  `SqlAlchemyBotRegistry` из `cli/wizard.py`, включая посев
  `gmq_bot_claim_state` — контракт 001): `contains(slug) -> bool`,
  `add(slug, token_half, pair_nonce)` (одна транзакция: bots INSERT +
  claim_state on_conflict_do_nothing), `fetch_half(slug) ->
  (token_half, pair_nonce) | None`, `delete(slug) -> bool` (одна
  транзакция: claim_state DELETE + bots DELETE).

**Checkpoint**: ядро готово; US1 и US2 могут стартовать параллельно.

---

## Phase 3: User Story 1 - Создание бота POST-запросом с автогенерацией slug (Priority: P1) 🎯 MVP

**Goal**: оператор создаёт бота через окно бутстрапа: токен из секретной
настройки, slug генерирует система, обе шифрованные половины записаны,
ответ содержит только slug.

**Independent Test**: при включённом окне один `POST /bootstrap/bots`
с именем → 201 + slug; в файле и строке — шифртексты и nonce, plaintext
отсутствует; повторный POST с тем же токеном → 409.

### Tests for User Story 1

- [x] T009 [P] [US1] Тесты `tests/unit/test_bot_admin.py`: дедуп-скан
  (живой бот с тем же id → конфликт; файл без строки → лечение под
  прежним slug); slug-уникальность через is_taken (файл + реестр);
  права файла; провал записи строки → компенсация не требуется (файл
  остаётся «неполной парой»). Пишутся до реализации (RED).
- [x] T010 [P] [US1] Тесты `tests/unit/test_bot_bootstrap.py`:
  эндпоинт на httpx ASGITransport с фейковым provisioner — 404 (флаг
  выкл.), 201 + slug, 400 (имя: пустое/`>64`/управляющие), 409, 503
  (токен или мастер-ключ не задан/битый — одна формулировка), 500
  (сбой записи); в ответах и логах нет токена/половин/nonce.

### Implementation for User Story 1

- [x] T011 [US1] Создать `src/gram_mq/bot_admin.py`: протокол
  `BotRegistry` (перенос из `cli/wizard.py`) и
  `provision_bot(bots_dir, registry, master_key, token, name) ->
  Provisioned(slug) | DuplicateToken | InvalidInput` — дедуп-скан
  файлов (`read_bot_file` + `decrypt_half`, извлечение числового id),
  лечение неполной пары (перезапись обеих половин свежим nonce под slug
  из имени файла + дописывание строки), генерация slug
  (`generate_bot_slug(id, is_taken=файл+реестр)`), шифрование половин,
  порядок «файл → строка» (R8), IntegrityError → повтор slug.
- [x] T012 [P] [US1] Схема `src/gram_mq/api/schemas.py`: `BotCreateIn`
  (поле `name`: непустое, ≤ 64, без управляющих — правила
  `_validate_name` визарда; нарушение → 400).
- [x] T013 [US1] `src/gram_mq/api/app.py`: снапшот `BotManagement(enabled,
  token: SecretStr | None)` (по образцу `KeyBootstrap`), порт
  `BotProvisioner` (callable), маршрут `POST /bootstrap/bots` с кодами
  контракта contracts/bot-bootstrap-endpoint.md; логирование — имя/slug
  и исход, без секретов.
- [x] T014 [US1] `src/gram_mq/api/main.py`: сборка — `parse_master_key`
  из настроек, `bots_dir`, замыкание provisioner'а над
  `SqlAlchemyBotRegistry` + `token_store`; отсутствующий мастер-ключ →
  путь 503.

**Checkpoint**: US1 работает независимо (проверяется quickstart §2).

---

## Phase 4: User Story 2 - Доставка со склеиванием половин (Priority: P2)

**Goal**: воркер расшифровывает обе половины, проверяет nonce парности,
склеивает токен в памяти и отправляет; повреждение/рассинхрон —
локальный отличимый отказ без обращения к Telegram.

**Independent Test**: бот с корректными половинами доставляет сообщение;
порча одного байта файловой половины → ретрай с причиной «bot config
unreadable (file/keys)»; удаление строки реестра → немедленный
dead-letter «bot not registered».

### Tests for User Story 2

- [x] T015 [P] [US2] Тесты `tests/unit/test_bot_config.py`: Ready —
  корректная склейка `id:secret`; NotRegistered — строки нет;
  NotReady — файла нет / NULL-колонки / мастер-ключ не задан / битый
  блоб / nonce не совпал / склейка невалидна.
- [x] T016 [P] [US2] Тесты `tests/unit/test_runner.py` (дополнить
  существующие): NotRegistered → dead_letter (permanent, «bot not
  registered»); NotReady → retry с отличимой причиной; Ready —
  отправка как раньше.

### Implementation for User Story 2

- [x] T017 [US2] `src/gram_mq/worker/bot_config.py`: тип
  `TokenLookup` (`Ready(token)` | `NotRegistered` | `NotReady`) в
  порте `BotConfig`; адаптер `SplitFileBotConfig(bots_dir,
  session_factory, master_key)` реализует `token_for` по таблице
  разрешения contracts/token-storage.md (реестр через
  `SqlAlchemyBotRegistry.fetch_half`, файл через `token_store`,
  склейка + финальная `validate_token_format`); `FileBotConfig`
  удаляется.
- [x] T018 [US2] `src/gram_mq/worker/runner.py`: маппинг TokenLookup —
  `NotRegistered` → `broker.dead_letter(..., reason="bot not
  registered")` (permanent, немедленно); `NotReady` →
  `_retry_or_fail(reason="bot config unreadable (file/keys)")`.
- [x] T019 [US2] `src/gram_mq/worker/main.py`: сборка
  `SplitFileBotConfig` (bots_dir из Settings, session factory,
  мастер-ключ из Settings).
- [x] T020 [US2] Интеграционные тесты
  `tests/integration/test_bot_lifecycle_db.py` (гейт
  GRAMMQ_TEST_DATABASE_URL): применить миграции; provision через
  `bot_admin`; enqueue → claim → склейка через SplitFileBotConfig →
  fake-sender ack; легаси-строка с NULL-колонками → NotReady;
  enqueue неизвестного бота → UnknownBotError (после снятия FK).

**Checkpoint**: US1 + US2 вместе дают сквозную доставку (quickstart §2–3).

---

## Phase 5: User Story 3 - Ревок бота отдельным запросом (Priority: P3)

**Goal**: ревок через `DELETE /bootstrap/bots/{slug}` удаляет строку,
claim-state и файл; очередь бота уходит в dead-letter с причиной; новые
сообщения отклоняются.

**Independent Test**: создать бота (US1), поставить сообщение, DELETE →
200; сообщение в dead-letter с «bot not registered»; `POST
/api/v1/messages` к slug → 404; повторный DELETE → 404.

### Tests for User Story 3

- [x] T021 [P] [US3] Тесты `tests/unit/test_bot_bootstrap.py`
  (дополнить): DELETE 200 (фейковый revoker), 404 (флаг выкл. / slug
  неизвестен); в ответах только факт и slug.

### Implementation for User Story 3

- [x] T022 [US3] `src/gram_mq/bot_admin.py`: `revoke_bot(bots_dir,
  registry, slug) -> bool` — транзакция удаления claim_state + bots
  (через `registry.delete`), затем unlink файла (провал unlink не
  откатывает ревок — осиротевший файл перехватит дедуп-скан, R9);
  неизвестный slug → False.
- [x] T023 [US3] `src/gram_mq/api/app.py` + `src/gram_mq/api/main.py`:
  маршрут `DELETE /bootstrap/bots/{bot_slug}` под тем же флагом
  (`BotRevoker` callable), коды 200/404, логирование без секретов;
  сборка revoker в api/main.py.
- [x] T024 [US3] Интеграционные тесты
  `tests/integration/test_bot_lifecycle_db.py` (дополнить): ревок при
  непустой очереди → сообщение `status='failed'`, `error='bot not
  registered'` (немедленно, без исчерпания попыток); строки
  bots/claim_state удалены; файл удалён; повторный DELETE → 404;
  enqueue к slug после ревока → UnknownBotError.

**Checkpoint**: полный жизненный цикл бота (quickstart §4).

---

## Phase 6: Polish & Cross-Cutting Concerns

- [x] T025 Выпилить CLI-визард (FR-017, R11): в `src/gram_mq/cli/main.py`
  удалить подкоманды `bot add`/`bot list` и всю ветку wizard
  (exit-коды 2/3/4/130 для ботов устаревают; `key add`/`key prepare`
  не трогаются); удалить файлы `src/gram_mq/cli/wizard.py` и
  `src/gram_mq/cli/token.py` (их логика переехала в `token_store.py` /
  `bot_registry.py` / `bot_admin.py`); удалить тесты визарда, обновить
  импорты переносимых тестов.
- [x] T026 [P] Задокументировать переменные в `.env.example`:
  `GRAMMQ_BOT_BOOTSTRAP_ENABLED` / `GRAMMQ_BOT_BOOTSTRAP_TOKEN` (окно:
  задать токен → включить → редеплой → POST/DELETE → выключить и
  **очистить токен**) и `GRAMMQ_TOKEN_MASTER_KEY` (генерация
  os.urandom(32).hex(); потеря = перевыпуск токенов BotFather).
- [x] T027 Прогнать `specs/006-encrypted-token-storage/quickstart.md`
  целиком на локальном окружении; найденные дефекты исправить.
- [x] T028 Финальный гейт: `uv run check` (ruff + mypy --strict)
  без ошибок; `uv run test` — покрытие ≥ 80%; убедиться, что
  plaintext-токена нет ни в одном тестовом артефакте.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 → Phase 2**: зависимость `cryptography` нужна T002.
- **Phase 2**: блокирует все истории (крипто-ядро, slug, настройки,
  схема, enqueue, реестр).
- **US1 и US2**: после Phase 2 независимы и могут идти параллельно
  (US1: api-сторона; US2: worker-сторона; общий только
  `bot_registry.py`, уже готовый в T008).
- **US3**: после US2 (маппинг NotRegistered→dead_letter в T018 делает
  ревок наблюдаемым в очереди) и после US1 (паттерн эндпоинта; оба
  маршрута в одном `api/app.py` — правки в один файл, последовательно).
- **Polish**: после всех историй; T025 меняет `cli/`, которого истории
  уже не касаются.

### User Story Dependencies

- **US1 (P1)**: Phase 2 → всё своё.
- **US2 (P2)**: Phase 2 → всё своё (проверяем на половинах, созданных
  тестовыми средствами, без US1).
- **US3 (P3)**: Phase 2 + US2 (наблюдаемость dead-letter) + US1
  (эндпоинт-паттерн).

### Within Each User Story

- Тесты (T009/T010, T015/T016, T021) пишутся до реализации и падают
  (RED), затем реализация до зелёного.
- Модели/чистые модули → адаптеры → эндпоинты/воркер → интеграционные.

### Parallel Opportunities

- Phase 2: T002+T003 ∥ T004 ∥ T005 (разные файлы); T006 → T007
  (обе про схему/enqueue, но разные файлы — параллельно после T006
  нельзя: T007 тестируется при снятом FK — строго после T006); T008
  параллелен T006/T007.
- US1: T009 ∥ T010 (разные файлы тестов); T012 ∥ T011; затем T013 → T014.
- US2: T015 ∥ T016; T017 → T018 → T019; T020 после T017.
- US3: T021 до T022/T023.

---

## Parallel Example: User Story 1

```bash
# Тесты параллельно (разные файлы):
Task T009: "tests/unit/test_bot_admin.py"
Task T010: "tests/unit/test_bot_bootstrap.py"

# Затем реализация:
Task T011: "src/gram_mq/bot_admin.py"        # ядро создания
Task T012: "src/gram_mq/api/schemas.py"       # параллельно с T011
Task T013: "src/gram_mq/api/app.py"           # после T011–T012
Task T014: "src/gram_mq/api/main.py"          # после T013
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Phase 1 + Phase 2 → ядро (крипто, схема, настройки).
2. Phase 3 (US1) → создание ботов через окно бутстрапа работает.
3. **STOP and VALIDATE**: quickstart §2 — половины записаны, plaintext
   отсутствует, 409 на повтор.

### Incremental Delivery

1. Foundation → US1 (MVP) → US2 (сквозная доставка) → US3 (жизненный
   цикл) → Polish (CLI-выпиливание, документация, гейты).
2. Каждая история не ломает предыдущие: US1 не трогает worker, US2 не
   трогает api-маршруты, US3 добавляет один маршрут.

### Parallel Team Strategy

- Разработчик A: US1 (api: bot_admin, app, main, schemas).
- Разработчик B: US2 (worker: bot_config, runner, main, интеграция).
- Затем вместе: US3 + Polish.

---

## Notes

- [P] = разные файлы, нет зависимостей от незавершённых задач.
- Одна фича — один коммит после приёмки ревьюером (house rule).
- Интеграционные тесты (T020/T024) пропускаются без
  `GRAMMQ_TEST_DATABASE_URL` — гейт не блокирует unit-прогон.
- `uv run check` и `uv run test` — обязательные гейты перед ревью (T028).
