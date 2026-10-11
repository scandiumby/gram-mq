# Contract: BotConfig (порт домена)

**Date**: 2026-10-11 | **Spec**: [spec.md](spec.md) | Конституция I: воркер
зависит от порта, а не от файловой системы.

## Интерфейс

```python
class BotConfig(Protocol):
    async def token_for(self, bot_slug: str) -> str | None:
        """Return the bot token `{id}:{secret}` or None when the bot has
        no readable, format-valid config file."""
```

## Семантика

- Единственный источник токена — файл `bots/.env-<bot_slug>` (фича 002,
  конституция III): ключ GRAMMQ_BOT_TOKEN, формат `{id}:{secret}`.
- None означает «файла нет ИЛИ содержимое повреждено» — различие не
  раскрывается: для воркера это одна и та же retryable-ошибка отправки
  (spec FR-007/FR-008, MVP-дефолт Session 2026-10-11).
- Каталог bots/ берётся из Settings (GRAMMQ_BOTS_DIR, default ./bots) —
  тот же канал, что у CLI фичи 002; права 0700/0600 проверяются на
  записи (002), рантайм-проверка прав не дублируется.
- Токен не кэшируется портом: каждый claim читает файл — перевыдача
  токена подхватывается следующей попыткой доставки без рестарта воркера.
- Адаптер `FileBotConfig` переиспользует парсер `read_token_file` фичи 002
  (единый контракт формата); токен существует как строка только в памяти
  воркера и в аргументе TelegramSender.
