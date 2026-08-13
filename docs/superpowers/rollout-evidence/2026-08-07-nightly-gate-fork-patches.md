# Nightly-gate upstream carry ledger

Local fork patches made while restoring the merged `tests/events`, `tests/cron`, and
`tests/gateway` suites to green. Re-check and re-apply these entries after each upstream
merge; upstream-owned files may be replaced wholesale.

| File | Upstream owner evidence | Local patch | Why / regression guard |
|---|---|---|---|
| `plugins/platforms/whatsapp/adapter.py` | Last upstream boundary: `98c3f210e merge: upstream v2026.7.20 (0.19.0) into fork` | Removed class-local `_is_dm_allowed` / `_is_group_allowed` copies so `WhatsAppAdapter` inherits the hardened gates from `gateway/platforms/whatsapp_common.py`; changed the DM pre-processing gate to `_is_dm_intake_allowed` so pairing handshakes can reach downstream strict authorization. | The stale local copies shadowed the mixin and made `pairing` fail open (`return True`) for unknown DMs. Guard: `tests/gateway/test_config_driven_access_policy.py::test_whatsapp_adapter_does_not_shadow_hardened_access_helpers`; full file verified `72 passed` on 2026-08-06. |
| `gateway/platforms/base.py` | Upstream-owned gateway base module, carried through the `98c3f210e` upstream boundary. | Reject an embedded NUL before passing a normalized `MEDIA:` path to `os.path.expanduser()`. | Crafted `~\x00...` tags raised `ValueError` on Windows and aborted all media extraction. Existing RED guard: `tests/gateway/test_platform_base.py::TestMediaSecurityRegressionTests::test_extract_media_tolerates_crafted_null_path`; full file verified `192 passed, 4 skipped` on 2026-08-06. |
| `plugins/platforms/feishu/adapter.py` | Plugin-migrated upstream adapter; SDK names are initialized to `None` before optional import. | Check `CreateMessageRequestBody` and `CreateMessageRequest` for `is not None` instead of testing whether their names exist in `globals()`. | The names always exist, so SDK-absent fallback tests called `.builder()` on `None`. Guards: `tests/gateway/test_stream_consumer_thread_routing.py`; full file verified `9 passed` on 2026-08-06. |
