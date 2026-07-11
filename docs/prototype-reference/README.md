# Prototype reference scripts

Engine scripts from the OpenClaw prototype ("silver-claw"), kept as **reference
only** — the product's scripts will be written from scratch against the
provider-adapter architecture (PRODUCT.md §3). These show the working patterns
and hard-won edge cases; they are NOT runnable (secrets replaced with
`{{PLACEHOLDER}}` tokens, legacy EWS/Sheets/CLI transports).

One script per pattern, organized by the product module it prototypes:

| Dir | Script | Pattern it demonstrates | Product feature |
|---|---|---|---|
| `email/` | `send_aa_batch.py` | Generic multi-language batch sender with eligibility filtering | §7.19 send-bulk |
| `email/` | `saudi_multiagent_v2.py` | Researcher→Composer→Sender multi-agent pipeline, token-budgeted workers | §7.24 agent runs |
| `email/` | `send_with_research.py` | One-at-a-time send with inline research + pacing | §7.16 custom-lead flow |
| `replies-bounces/` | `check-customer-responses.py` | Inbox polling → reply detection → operator alert + status update | §7.19 replies |
| `replies-bounces/` | `full_bounce_check.py` | Post-batch MAILER-DAEMON scan → bounce marking | provider `get_message_status` |
| `qa/` | `preflight_check.py` | Pre-send QA gate: language purity, double-dash, placeholder scan | outreach approval gate |
| `qa/` | `sheet_validator.py` | Contact-data validation (email regex, country/phone mismatch) | §6.5 contact upload validation |
| `reporting/` | `daily-report.sh` | End-of-day outreach report assembly | §7.25 analytics |
| `leads/` | `research_prompt_v2.txt` | Validated research-worker prompt + output contract | §7.13 research runs |
| `linkedin/` | `linkedin_cleanup_ledger.py` | Deterministic state-machine ledger for long-running batch jobs | pattern only — LinkedIn automation is NOT in the product (PRODUCT.md §5) |

Durable lessons encoded in these scripts (also captured in `skills/sales/`):
one send per company with CC-all; a transport timeout is not a delivery
failure — verify before retry; research workers never send; every batch
filters eligibility before composing, not after.
