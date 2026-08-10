# finance_records plugin

`finance_records` records a narrow, deterministic set of Japanese Telegram finance messages to a cashflow/repayment sheet workflow. It is safe by default: it only handles explicitly allowed Telegram chats, uses `Asia/Tokyo` and `JPY`, runs in dry-run mode by default, and refuses live Google Sheets writes unless live mode is explicitly enabled and service-account prerequisites are present.

## What it recognizes

Examples supported by the deterministic parser:

- `今日シェアフルで6,000円稼いだ。今日入金済み` → received income
- `今日シェアフルで6,000円稼いだ。8月15日入金予定` → scheduled income
- `今日松屋で750円使った` → expense
- `今日ぼんに70,000円返した` → cashflow repayment plus repayment history linkage
- `さっきの6,000円は5,800円だった` → correction audit row referencing the original record
- `さっきの記録を取り消して` → cancellation audit row referencing the original record
- `今月あといくら足りない？` → current-month shortage query
- `直近の記録を5件見せて` → latest five records query

Ambiguous income such as `今日シェアフルで6,000円稼いだ` is not recorded. Hermes replies:

`その6,000円は、もう使える状態で入金されていますか？　それとも後日入金ですか？`

## Environment variables

- `FINANCE_ALLOWED_TELEGRAM_CHAT_IDS`: comma-separated Telegram chat IDs. If unset, the plugin does not process messages.
- `FINANCE_TIMEZONE`: defaults to `Asia/Tokyo`.
- `FINANCE_DRY_RUN`: defaults to `true`. Keeps writes in the fake/dry-run adapter.
- `FINANCE_SHEETS_ENABLED`: defaults to `false`. Must be `true` with `FINANCE_DRY_RUN=false` before live Sheets mode is even attempted.
- `CASHFLOW_SPREADSHEET_ID`: defaults to `1QXrtN2MVNfvjIFYUlcpuI8yEPjrFDLCep07eMYOcc0Q`.
- `REPAYMENT_SPREADSHEET_ID`: defaults to `1ufERJYzKAMZUoErVFXN02eEXdzd4FHivS17gCrFNoiI`.
- `GOOGLE_APPLICATION_CREDENTIALS`: path to a Google service-account JSON file for live mode. Do not commit or print this file.

## Google service account setup

1. Create or choose a Google Cloud project.
2. Enable the Google Sheets API.
3. Create a service account with the minimum permissions needed for the target spreadsheets.
4. Download the service-account JSON to a private local path outside the repository.
5. Share both spreadsheets with the service account email.
6. Set `GOOGLE_APPLICATION_CREDENTIALS` to the private JSON path.
7. Keep `FINANCE_DRY_RUN=true` until you have verified parser behavior and workbook mappings.

Live writes currently fail closed unless fixed SheetLayout mappings are confirmed in code. This prevents accidental writes to guessed tabs/ranges.

## Dry-run to live switch

Recommended dry-run configuration:

```bash
FINANCE_ALLOWED_TELEGRAM_CHAT_IDS="123456789"
FINANCE_TIMEZONE="Asia/Tokyo"
FINANCE_DRY_RUN="true"
FINANCE_SHEETS_ENABLED="false"
```

Live-mode prerequisites:

```bash
FINANCE_ALLOWED_TELEGRAM_CHAT_IDS="123456789"
FINANCE_TIMEZONE="Asia/Tokyo"
FINANCE_DRY_RUN="false"
FINANCE_SHEETS_ENABLED="true"
GOOGLE_APPLICATION_CREDENTIALS="/private/path/to/service-account.json"
```

Do not store secrets in the repository. Do not paste service-account JSON into logs or chat.

## Audit trail

The cashflow spreadsheet uses the `Hermes入力履歴` audit tab with fixed headers:

```text
event_id, telegram_chat_id, telegram_message_id, received_at, type, occurred_date, payment_date, source, creditor, amount, status, raw_text, correction_of, processing_status, error, created_at
```

The idempotency key is `telegram_chat_id + telegram_message_id`. Duplicate Telegram deliveries are acknowledged but not double-counted.

Corrections and cancellations append audit rows and reference the original `event_id`; existing audit rows are not silently deleted.

## Rollback / disable

Fast rollback options:

- Remove the plugin from `plugins.enabled` if you explicitly enabled it in `config.yaml`.
- Unset `FINANCE_ALLOWED_TELEGRAM_CHAT_IDS` so no chat is processed.
- Set `FINANCE_SHEETS_ENABLED=false`.
- Set `FINANCE_DRY_RUN=true`.

The safest emergency switch is:

```bash
FINANCE_SHEETS_ENABLED="false"
FINANCE_DRY_RUN="true"
```
