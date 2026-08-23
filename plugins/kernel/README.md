# kernel (bundled plugin)

MershLab's own audit invariant for the harness — the piece named across
every design doc this session as "the single most-referenced not-yet-built
piece" (`internal-docs/harness/STATE.md`, private repo). Observes every
outgoing model call through Hermes's real `pre_api_request` /
`post_api_request` hooks (`hermes_cli/plugins.py` `VALID_HOOKS`), not a
new mechanism bolted onto the agent loop.

## What it actually checks, stated precisely

Two external precedents shaped this, both surveyed in depth before any
code was written: DeepSeek Harness's `deriveMessages()`/`invariant.ts`
(byte-exact request reconstruction from an append-only log) and
OpenClaw's `enforcement.coverageState` gradient (an honest confidence
label on whether a check actually enforced anything, not just
pass/fail). This plugin does **not** implement DeepSeek's full
byte-exact replay — that needs an independent event log walking every
user/tool/assistant event, which needs hook coverage Hermes's plugin
architecture doesn't expose (`pre_llm_call` fires for context injection,
not full message assembly). What it does implement, honestly scoped to
what `pre_api_request`/`post_api_request` actually expose: **a session's
message history must never silently shrink between two consecutive
outgoing calls.** A truncation bug, a corrupted cache reload, or a race
condition clobbering context all show up as an unexplained drop in
`message_count` — this catches exactly that, nothing more claimed.

## Detects, does not block

Hermes's hooks are observer-only by design — `PluginManager.invoke_hook`
wraps every callback in its own try/except and only logs a warning on
failure, never propagates it (`hermes_cli/plugins.py`). Nothing
registered here can stop a call in flight. `coverage_state` is therefore
never `enforced` in the OpenClaw sense; the strongest honest value this
module produces is `attribution-only` — detected and recorded, loudly
(a `kernel_violation` log entry plus a stderr line), not prevented.
Blocking would need a core patch to `agent/conversation_loop.py`'s hook
call sites — a real, named follow-up, not built here.

## What it doesn't take a dependency on, and why

MershLab already ships a real provenance mechanism for a different
product — `mershtrust.adapters.llm_adapter` (`request_hash`/
`response_hash`/`provenance_hash`, tiered from `self_attested` up to
`zk_inference`). This plugin mirrors that same vocabulary in plain
stdlib (`hashlib` + `json`) rather than importing `mershtrust` itself,
because importing it pulls in `numpy` for zero-knowledge/TEE machinery
this specific check has no use for — an unnecessary dependency for the
harness to carry for one hash function.

## Storage

Append-only JSONL at `$HERMES_HOME/kernel/events.jsonl` — one line per
`api_request`/`api_response`/`kernel_violation` event, matching every
other bundled plugin's `$HERMES_HOME`-scoped state convention (see
`plugins/contrib-screen/`).

`kind: backend` in `plugin.yaml` means this loads automatically, no
`hermes plugins enable` step.
