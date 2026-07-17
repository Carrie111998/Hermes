# Missing Platform Plugin Shims — Known Issue (undocumented until now)

**Status:** Open — documented but not yet fixed, per explicit decision during a
2026-07-09 cleanup pass (`claude/hermes-errors-xh2x9x`).

## Problem

`hermes_cli/gateway.py::_PLATFORMS` used to carry inline setup-wizard metadata
(setup instructions, `vars` schemas, token env vars) for every messaging
platform. Commit `e39b468` ("fix(gateway): source environment env and
auto-wrap dispatch tool args") removed the inline entries for **telegram,
slack, matrix, whatsapp, email, sms, wecom, and feishu**, replacing each with
a comment claiming it "moved to `plugins/platforms/<name>/` — setup metadata
discovered dynamically via the platform registry entry registered by
`plugins/platforms/<name>/adapter.py::register()`."

Those 8 plugin directories were never created. `git log --all` shows zero
history for any of `plugins/platforms/{telegram,slack,matrix,whatsapp,email,
sms,wecom,feishu}/`. The directories that *do* exist under `plugins/platforms/`
are: `discord`, `google_chat`, `irc`, `line`, `ntfy`, `simplex`, `teams` — all
successfully discovered via `platform_registry`.

## Impact

- **Gateway message routing is NOT affected.** The real adapter
  implementations (`gateway/platforms/telegram.py`, `slack.py`, `matrix.py`,
  `whatsapp.py`, `email.py`, `sms.py`, `wecom.py`, `feishu.py`) are all still
  present and presumably functional — existing users with env vars already
  configured should be unaffected.
- **`hermes setup gateway`'s interactive picker no longer lists these 8
  platforms at all** — new users cannot configure Telegram (or any of the
  other 7) through the setup wizard; they'd have to hand-edit env vars with
  no guided setup_instructions.
- **`_platform_status()` / `_all_platforms()`-driven status displays** (setup
  wizard, possibly other menus) silently omit these 8 platforms rather than
  showing "not configured".

## Evidence / repro

```
python3 -c "
from hermes_cli.plugins import discover_plugins
discover_plugins()
from gateway.platform_registry import platform_registry
print(sorted(e.name for e in platform_registry.all_entries()))
"
# -> ['discord', 'google_chat', 'irc', 'line', 'ntfy', 'simplex', 'teams']
# telegram/slack/matrix/whatsapp/email/sms/wecom/feishu are absent
```

Test files currently failing because of this (all pre-existing failures,
not caused by this cleanup pass):

- `tests/hermes_cli/test_gateway_platform_gating.py` —
  `TestMatrixHiddenOnWindows::test_matrix_present_on_linux`,
  `test_matrix_present_on_macos`, `test_other_platforms_unaffected_on_windows`
  (asserts telegram/matrix are in the picker; they aren't, on any platform)
- `tests/hermes_cli/test_setup.py` —
  `test_setup_gateway_skips_service_install_when_systemctl_missing`,
  `test_setup_gateway_in_container_shows_docker_guidance` (both rely on
  Matrix showing as "configured" in the picker so `setup_gateway()` reaches
  its "Messaging platforms configured!" branch)
- `tests/hermes_cli/test_setup_openclaw_migration.py::TestGetSectionConfigSummary` —
  `test_gateway_lists_platforms` (expects "Telegram" in the openclaw-migration
  config summary; only "Discord" shows), `test_gateway_recognises_whatsapp_enabled`
  (WhatsApp isn't recognized at all, summary is `None`)
- `tests/gateway/test_setup_feishu.py` — all 14 tests fail with
  `ImportError: cannot import name '_setup_feishu' from 'hermes_cli.gateway'`.
  This one's slightly different: the function itself (not just the picker
  metadata) was removed from `gateway.py` with a comment claiming it "moved to
  plugins/platforms/feishu/adapter.py::interactive_setup" — that function
  doesn't exist anywhere either. Whichever option below is chosen for Feishu
  needs to restore an `interactive_setup`-equivalent entry point, not just a
  `register()` metadata shim.

## Options considered (not yet decided)

1. **Restore Telegram only** as a proof of concept, matching
   `plugins/platforms/discord/adapter.py`'s registration pattern, and follow
   up on the other 7 separately.
2. **Restore all 8** plugin adapter shims — larger effort, 8 new files, each
   needs to correctly reproduce the removed `vars`/`setup_instructions`/
   `install_hint`/`is_connected`/`check_fn` contract without introducing
   drift from the real `gateway/platforms/*.py` adapters.
3. **Revert to inline `_PLATFORMS` definitions** for these 8 — lowest risk,
   but reverses whatever the plugin-registry refactor was meant to
   accomplish (see `e39b468`'s commit message/diff for the rest of the
   intended architecture).
4. Something else — worth checking with whoever authored `e39b468` whether
   the plugin shims exist in an unpushed branch/local checkout, since the
   commit message describes them as already done.

## Next step

Needs an explicit decision on which option above before someone picks this
up — flagged here per user request rather than acted on unilaterally.
