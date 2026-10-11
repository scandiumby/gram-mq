# Research: HTTP API приёма сообщений и доставка в Telegram

**Date**: 2026-10-11 | **Status**: неясности спеки закрыты (MVP-дефолты в
spec.md, Session 2026-10-11); здесь — решения плана.

## R1. Проверка «неизвестный бот» — самим брокером, без отдельного запроса

- **Decision**: API не делает SELECT по реестру; контракт `BrokerPort.enqueue`
  сам поднимает `UnknownBotError` (FK по bot_slug — 001), API маппит его в
  404. Memory-адаптер ведёт себя так же (register_bot).
- **Rationale**: один round-trip вместо двух; реестр остаётся единственным
  источником истины; повторное использование уже контрактно протестированного
  поведения.
- **Alternatives**: предварительный SELECT бота — лишний запрос и гонка
  (бот может быть удалён между проверкой и вставкой).

## R2. Формат API-ключа и prefix

- **Decision**: ключ `gmq_` + `secrets.token_urlsafe(32)` (43 символа
  base64url, итого 47); prefix — первые 12 символов ключа (включая `gmq_`),
  первичный ключ таблицы.
- **Rationale**: prefix уникален с вероятностью ~2^48 на запись и не даёт
  перебора остатка (в base64url 8 случайных символов после `gmq_`);
  lookup по prefix — индексный, Argon2id-верификация — ровно одна на ключ.
- **Alternatives**: отдельный случайный prefix + ключ — два источника
  случайности без выгоды; prefix короче 12 — растёт число коллизий
  (безвредно, но усложняет поддержку).

## R3. Argon2id: параметры и место вызова

- **Decision**: `argon2-cffi` `PasswordHasher` с дефолтами (Argon2id,
  64 MiB, t=3) в runtime; верификация — `asyncio.to_thread` в
  `ApiKeyVerifier.verify` (комментарий-обоснование по конституции,
  Asynchrony). В тестах верификатор принимает инжектируемый hasher с
  дешёвыми параметрами (t=1, m=64 KiB) — тесты остаются быстрыми, а
  продакшн-параметры не ослабляются.
- **Rationale**: дефолты argon2-cffi — актуальные рекомендации PHC для
  паролей; ключ высокоэнтропийный, запас не нужен, но и цена мала; в event
  loop блокировка ~50 мс недопустима — to_thread.
- **Alternatives**: HMAC-SHA256 (быстрее) — конституция резервирует HMAC
  для OTP/refresh, ключи — Argon2id (III); пониженные параметры в проде —
  ослабление без нужды.

## R4. Таксономия ошибок отправки и политика повторов

- **Decision**: `TelegramSendError` несёт `retry_after: float | None` и
  `permanent: bool`. Воркер: permanent (400/403 Telegram: чат не найден,
  бот заблокирован) → `dead_letter` немедленно (это и есть статус `failed`
  с причиной — контракт 001); retryable (сеть, таймаут, 5xx) → `retry` с
  экспоненциальным backoff `base * 2**attempts` (cap 300 с); 429 →
  `retry` с задержкой `max(retry_after, backoff)`. Отсутствие/повреждение
  файла токена — retryable (файл может появиться), после max_attempts —
  failed через штатный путь брокера.
- **Rationale**: спека (FR-007) требует retry-путь и failed после
  max_attempts для ретраебельных ошибок; permanентные ретраить бессмысленно
  — dead_letter это тот же терминальный `failed`, но без пяти холостых
  попыток. Backoff-кап 300 с держит журнал «живым».
- **Alternatives**: всё через retry → failed — холостые попытки и часы
  мусора для «chat not found»; мгновенный dead_letter для сетевых —
  потеря доставимости при мигании сети.

## R5. Как воркер получает токен: порт BotConfig

- **Decision**: порт `BotConfig` (`token_for(bot_slug) -> str | None`),
  адаптер `FileBotConfig` переиспользует парсер фичи 002
  (`gram_mq.cli.token.read_token_file`) поверх `bots/.env-<slug>`;
  `bots_dir` — из Settings (GRAMMQ_BOTS_DIR). Токен собирается обратно в
  `{id}:{secret}` только в памяти воркера и передаётся в TelegramSender.
- **Rationale**: конституция I перечисляет BotConfig среди портов; парсер
  формата уже существует и протестирован (002); файл — единственное место
  хранения токена (III).
- **Alternatives**: читать файл напрямую в воркере — домен зависел бы от
  файловой системы; хранить токен в БД — запрещено (III).

## R6. Aiogram-адаптер

- **Decision**: `AiogramTelegramSender` держит `aiogram.Bot` по токену
  (кэш в словаре), `send_message(chat_id=..., text=text)` без parse_mode;
  исключения маппятся чистой функцией `classify`: `TelegramRetryAfter` →
  retry_after, `TelegramNetworkError`/таймаут → retryable,
  `TelegramBadRequest`/`TelegramForbidden`/прочие API-ошибки → permanent.
  Закрытие — `aclose()` (закрывает сессии всех ботов).
- **Rationale**: конституция Stack: aiogram 3, `Bot` only; кэш бота по
  токену переиспользует сессию aiohttp; чистая функция классификации
  покрывается юнит-тестами без сети.
- **Alternatives**: Bot на каждый send — лишние сессии/рукопожатия;
  TelegramBadRequest как retryable — противоречит R4.

## R7. API: приложение и DI

- **Decision**: `create_app(*, broker: BrokerPort, key_verifier) -> FastAPI`;
  маршруты: POST /api/v1/messages (202 + {"id": ...}), GET /health
  ({"status": "ok"}, без авторизации). Авторизация — зависимость FastAPI,
  читающая только заголовок X-API-Key; 401 без деталей. Валидация тела —
  pydantic-схема MessageIn: bot_slug 1..64; chat_id
  `^-?\d{1,18}$ | ^@[A-Za-z0-9_]{4,64}$`; text 1..4096. Запуск:
  консольный скрипт `gram-mq-api` (uvicorn, factory), host/port из
  Settings (GRAMMQ_API_HOST/PORT, default 127.0.0.1:8080).
- **Rationale**: инъекция портов делает приложение тестируемым на
  memory-брокере и фейк-верификаторе без сети; 202 (не 201) — ресурс
  «доставка» ещё не создан, принято для асинхронных очередей; chat_id-паттерн
  покрывает числовые идентификаторы (в т.ч. групповые отрицательные) и
  @username.
- **Alternatives**: 201 — семантически неверно (сообщение не «создано» как
  ресурс доставки); auth-миддлвэрь на всё приложение — health пришлось бы
  исключать хаком.

## R8. Воркер: цикл и идентичность

- **Decision**: `run_worker(*, broker, sender, bot_config, poll_interval,
  backoff_base, worker_id)` — вечный цикл: claim → обработка одной доставки
  → сразу следующий claim (без сна, пока есть работа); пустой claim →
  `asyncio.sleep(poll_interval)`. worker_id = `f"{hostname}:{pid}"`.
  Точка входа `gram-mq-worker` собирает PostgresBroker +
  AiogramTelegramSender + FileBotConfig из Settings. poll_interval default
  1.0 с, backoff_base default 5.0 с (Settings, не секреты).
- **Rationale**: busy-loop исключён; worker_id нужен контракту claim
  (владение арендой); сборка адаптеров в `api/main.py` и `worker/main.py`
  — единственное место,
  где домен встречается с конкретными технологиями (конституция I).
- **Alternatives**: фиксированный worker_id — неверно при нескольких
  инстансах (Future); сон после каждой доставки — лишняя задержка.

## R9. Зависимости и лицензии

- **Decision**: добавить fastapi, uvicorn, argon2-cffi (runtime), httpx
  (dev). Лицензии: MIT/BSD-3/Apache-2.0 — совместимы. Транзитивно через
  aiogram: certifi (MPL-2.0).
- **Rationale**: конституция запрещает GPL/AGPL-семью; MPL-2.0 —
  file-scoped слабый копилефт сертификатного бандла, не ограничивает
  распространение MIT-проекта; трактовка «similar permissive» применена
  осознанно и зафиксирована здесь. Если владелец сочтёт MPL недопустимой —
  aiogram единственный источник certifi, замена отправителя не меняет порт.
- **Alternatives**: отказаться от aiogram — против конституции (Stack);
  hand-rolled Bot API client поверх aiohttp — больше кода без выгоды.

## R10. systemd-юниты

- **Decision**: `deploy/gram-mq-api.service` и `deploy/gram-mq-worker.service`:
  User=gram-mq, ExecStart=/opt/gram-mq/.venv/bin/gram-mq-api|gram-mq-worker,
  Restart=on-failure, единственная Environment=GRAMMQ_ENV_FILE=/etc/gram-mq/.env
  (указатель, без значений — конституция Configuration).
- **Rationale**: конституция Stack: systemd, api и worker отдельными
  юнитами; env-файл вне deploy-дерева; юниты — статические артефакты,
  путь venv — переменная деплоя (закомментированная альтернатива в файле).
- **Alternatives**: Docker/compose — запрещены конституцией; значения в
  юните — запрещены (Configuration).

## R11. Отзыв ключа без кэша

- **Decision**: верификация на каждом запросе: SELECT по prefix →
  revoked_at IS NULL → Argon2id-verify (to_thread). Кэширования нет —
  отзыв действует на следующем же запросе (FR-014).
- **Rationale**: один индексный SELECT + одна Argon2-верификация (~50 мс
  вне loop) на запрос; при десятках rps это дёшево, а семантика отзыва
  мгновенная.
- **Alternatives**: TTL-кэш prefix→bool — задержка отзыва, усложнение;
  для MVP и одного потребителя — не нужно.

## R12. Логи без секретов

- **Decision**: api и воркер логируют через logging на stderr (systemd
  journal подхватит): события уровня «принято/доставлено/повтор/failed» с
  bot_slug, chat_id, id сообщения и prefix ключа; токен бота и полный ключ
  не логируются ни при каких условиях; тексты исключений проксируются
  только после фильтрации — отправка текста ошибки не содержит секретов,
  т.к. они не попадают в исключения по построению (002: ошибки формата без
  токена; aiogram-ошибки не содержат токен).
- **Rationale**: FR-008/FR-009 и SC-004; journal — эксплуатационная
  наблюдаемость MVP без нового инструментария.
- **Alternatives**: structlog — новая зависимость без нужды MVP.
