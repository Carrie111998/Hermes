# RCA — Nous Portal sticky routing loses its key mid-conversation

Issue: #71576

## Summary

The Nous Portal profile publishes a top-level `session_id` in the request body.
That field is not bookkeeping: it is the **provider sticky routing key**. The
Portal uses it to pin every turn of a conversation to the same upstream
endpoint. Pinning matters because explicit Anthropic `cache_control`
breakpoints are **instance-local** (Anthropic, Vertex and Bedrock all behave
this way) — a reroute does not read a warm cache, it cold-writes a fresh one.

Two independent defects caused the key to change or disappear part-way through
a conversation, so the Portal fell back to hashing the opening messages and
rerouted. The cache was cold-written exactly when it was most expensive.

## Root cause

### 1. The sticky key tracked the segment, not the conversation

`NousProfile.build_extra_body` used only the explicit `session_id` argument,
which is `agent.session_id`.

* `agent.session_id` **rotates when context compression starts a new segment**.
  Using it alone silently re-keys sticky routing mid-conversation and
  cold-writes the cache right after the compression turn — the most expensive
  prompt of the session, since it carries the full pre-compaction history.
* Auxiliary call sites (compression, titles, vision, MoA slots) never pass
  `session_id` at all. They already carried the `conversation=` tag but no
  sticky key, so each one routed independently of the main conversation.

The `conversation=` tag emitted by `nous_portal_tags` already resolves the
right value — the **lineage ROOT id** published by the agent loop, which is
stable across compression segments. The sticky key and the tag disagreed.

### 2. Out-of-turn compaction ran with no conversation tag at all

`AIAgent._compress_context` is a forwarder. Several entry points call it
**outside** `run_conversation`'s ambient scope:

* `/compact` (`cli.py`)
* the gateway `/compress` command and its hygiene sweep — both build a
  throwaway agent
* partial head compression

With no ambient context there is no `conversation=` tag, so the Portal routes
by payload hash. The single largest prompt of the session (the full
uncompressed history) lands on a cold endpoint and drags the following turns
with it.

## Fix

**`plugins/model-providers/nous/__init__.py`** — resolve the sticky key exactly
the way `nous_portal_tags` resolves the `conversation=` tag: ambient context
first, explicit argument as fallback.

```python
sticky_key = get_conversation_context() or session_id
if sticky_key:
    body["session_id"] = sticky_key
```

This keeps the key stable across compression segments and gives auxiliary call
sites the same key as the conversation they belong to.

**`run_agent.py`** — publish the lineage root in the `_compress_context`
forwarder when nothing is ambient, and restore the previous value in `finally`
so a compaction never leaks its tag into the surrounding scope. In-turn callers
already have it set to the same value, so this is a no-op for them.

Both changes are fallback-shaped: they only add a value where one was missing
or wrong, and never override an ambient value set by the caller.

## Tests

Added to `tests/agent/test_portal_tags.py`:

| Test | Guards |
| --- | --- |
| `test_nous_sticky_key_matches_conversation_tag` | key tracks the lineage ROOT, not the rotating segment |
| `test_nous_sticky_key_falls_back_to_explicit_session_id` | outside a turn, the explicit `session_id` still wins |
| `test_compress_context_publishes_root_when_called_out_of_turn` | out-of-turn compaction still carries the tag |
| `test_compress_context_preserves_ambient_context` | in-turn compaction inherits the root and restores it untouched |

## Validation

97 passed across the routing, compression and portal-tag suites:

```
tests/agent/test_portal_tags.py
tests/agent/test_compression_concurrent_fork.py
tests/agent/test_compression_rotation_state.py
tests/agent/test_compression_logging_session_context.py
tests/agent/test_idle_compaction_lock_and_guards.py
tests/test_cli_manual_compress.py
```

### Note on an unrelated pre-existing failure

`tests/test_hermes_state_compression_locks.py` has two failing tests
(`test_non_expired_lock_from_dead_pid_is_reclaimed`,
`test_dead_pid_reclaim_via_os_kill_fallback_when_psutil_missing`). They were
confirmed red on a pristine checkout of `origin/main` (`4872033b2`), before any
change in this branch, and concern dead-PID reclaim in the compression lock —
a different subsystem. They are addressed separately and are **not** in scope
here.
