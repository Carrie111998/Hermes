# Finance records Telegram plugin

The bundled `finance_records` plugin safely records a narrow set of Japanese Telegram finance messages. It is conservative by default: only explicitly allowed Telegram chats are processed, the timezone defaults to `Asia/Tokyo`, currency is `JPY`, dry-run is on, and live Google Sheets writes are disabled/fail closed until workbook mappings are confirmed.

For full operational details see [`plugins/finance_records/README.md`](../../plugins/finance_records/README.md).

## Quick setup

Configure only non-secret flags and IDs in your environment or service manager:

```bash
FINANCE_ALLOWED_TELEGRAM_CHAT_IDS="123456789"
FINANCE_TIMEZONE="Asia/Tokyo"
FINANCE_DRY_RUN="true"
FINANCE_SHEETS_ENABLED="false"
```

Live mode additionally needs a private Google service-account JSON path in `GOOGLE_APPLICATION_CREDENTIALS`. Never commit, paste, or log that JSON file.

## Rollback

Disable processing by unsetting `FINANCE_ALLOWED_TELEGRAM_CHAT_IDS`, or force safe mode:

```bash
FINANCE_SHEETS_ENABLED="false"
FINANCE_DRY_RUN="true"
```
