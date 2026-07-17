# Missing Platform Plugin Shims — Resolved

**Status:** Closed. Resolved in two passes:

1. PR #89 (`fix(gateway): restore built-in platforms dropped from setup
   picker`) restored **telegram, slack, matrix, whatsapp, email, sms** as
   inline `_PLATFORMS` entries.
2. This pass restored the remaining four: **dingtalk, feishu, wecom, and
   wecom_callback** (the last two weren't in the original inventory below —
   found while re-auditing `_PLATFORMS` against its pre-refactor history).
   Feishu additionally needed its bespoke `_setup_feishu()` interactive
   function restored (QR-code onboarding), not just picker metadata — see
   below.

Option 3 from the original options list ("revert to inline `_PLATFORMS`
definitions") is what was chosen for all ten platforms, for both passes:
lowest risk, and consistent with the fact that every one of these adapters
(`gateway/platforms/*.py`) was never actually migrated to the plugin-registry
pattern the removed comments claimed.

## Original problem

`hermes_cli/gateway.py::_PLATFORMS` used to carry inline setup-wizard metadata
(setup instructions, `vars` schemas, token env vars) for every messaging
platform. Commit `e39b468` ("fix(gateway): source environment env and
auto-wrap dispatch tool args") removed the inline entries for telegram,
slack, matrix, whatsapp, email, sms, dingtalk, feishu, wecom, and
wecom_callback, replacing each with a comment claiming it "moved to
`plugins/platforms/<name>/`". Those plugin directories were never created —
`git log --all` showed zero history for any of them. The directories that
*do* exist under `plugins/platforms/` are: `discord`, `google_chat`, `irc`,
`line`, `ntfy`, `simplex`, `teams` (all genuinely plugin-registered).

Feishu was a deeper cut: the `_setup_feishu()` interactive function itself
(QR-code bot registration via `gateway/platforms/feishu.qr_register()`) was
deleted from `gateway.py`, not just its `_PLATFORMS` entry — a comment
claimed it moved to `plugins/platforms/feishu/adapter.py::interactive_setup`,
which never existed either.

## What was NOT restored

`_setup_telegram`/`_setup_slack`/`_setup_matrix`/`_setup_whatsapp`/
`_setup_dingtalk`/`_setup_wecom` bespoke interactive functions were **not**
recreated — PR #89 established the precedent of using the generic
vars-schema-driven `_setup_standard_platform()` flow for these instead
(simpler prompt-per-env-var UX vs. the original's mix of QR/OAuth flows).
This pass followed the same precedent for dingtalk, wecom, and
wecom_callback. Only Feishu got its bespoke function back, because
`tests/gateway/test_setup_feishu.py` (pre-existing, not written as part of
either fix) explicitly exercises the QR-registration UX and specific
`save_env_value` call sequence — the generic flow can't reproduce that.

## Verification

- `tests/hermes_cli/test_gateway_platform_gating.py`,
  `tests/hermes_cli/test_setup.py`,
  `tests/hermes_cli/test_setup_openclaw_migration.py`,
  `tests/gateway/test_setup_feishu.py` — all pass.
- `hermes_cli.gateway._all_platforms()` now returns all 23 platforms
  (10 restored/newly-added built-ins + mattermost/signal/weixin/bluebubbles/
  qqbot/yuanbao + the 7 genuine plugin-registry entries), confirmed via:
  ```
  python3 -c "
  import hermes_cli.gateway as gw
  print([p['key'] for p in gw._all_platforms()])
  "
  ```
- `ruff check hermes_cli/gateway.py` — clean.

## CodeQL alert on restored `_setup_feishu()`

Restoring `_setup_feishu()` reintroduced `save_env_value("FEISHU_APP_SECRET",
app_secret)` (gateway.py:5740), which CodeQL flags as a new high-severity
"clear-text storage of sensitive information" alert — it's new code in this
diff's eyes, even though it's byte-for-byte the same pattern already used,
unflagged, for every other credential in this file (e.g.
`save_env_value("WEIXIN_TOKEN", token)` at gateway.py:5540,
`save_env_value("QQ_CLIENT_SECRET", ...)` at gateway.py:5878).
`save_env_value()` (`hermes_cli/config.py:5107`) writes every credential in
the app to a plaintext `~/.hermes/.env` file protected by restrictive file
permissions (`_secure_file()`, 0600), never encryption — this is the CLI's
established, deliberate credential-storage design (same model as `aws`, `gh`,
git-credential-store), not a new vulnerability class introduced here.

Decision: dismiss the alert on GitHub's Security tab as a false positive
rather than change the storage architecture or add an unverified suppression
comment.
