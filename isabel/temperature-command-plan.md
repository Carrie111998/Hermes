# Implementation Plan — /temperature Slash Command (TUI + Discord)

**Author:** Isabel (draft for Rob's review)
**Date:** 2026-08-11
**Fork:** `/opt/data/src/hermes-agent-param-fix/` (hermes 0.20.0)
**Companion:** `isabel/implementation-plan.md` (the API-server `model_options.generation` patch — this command is the UI for that patch)

---

## 1. The Google Doc Was Half Right

The attached doc (from a Google chat) described:
- `hermes_cli/commands.py` → `COMMAND_REGISTRY` ✅ **real** — single source of truth for autocomplete/help on all surfaces
- `cli.py` → `process_command()` if-chain ✅ **real** — but the handler actually lives in `hermes_cli/cli_commands_mixin.py` (CLI mixin), not inline in cli.py
- `self.session.config.model.temperature = new_temp` ❌ **does not exist** — Hermes uses session-scoped override dicts, not a mutable config object
- Gateway/Discord dispatch ❌ **never mentioned** — Discord commands go through a *completely separate* path

**The real architecture (verified in fork):**

```
COMMAND_REGISTRY (hermes_cli/commands.py)
  ├── CLI surface: cli.py process_command() → hermes_cli/cli_commands_mixin.py handlers
  └── Gateway surface: gateway/run.py dispatch if-chain → gateway/slash_commands.py handlers
        └── session override plumbing (run.py) → agent request_overrides → API call
```

**The perfect template: `/reasoning`.** Session-scoped parameter, `--global` persistence, both surfaces, agent eviction on change. We mirror it exactly.

---

## 2. Files Touched (5 files, both surfaces)

| File | Change |
|---|---|
| `hermes_cli/commands.py` | Registry entry: `CommandDef("temperature", ..., aliases=("temp",), args_hint="[0.0-2.0] [--global]")` |
| `hermes_cli/cli_commands_mixin.py` | `_handle_temperature_command()` — CLI/TUI handler (mirror `_handle_reasoning_command`) |
| `cli.py` | One branch in `process_command()`: `elif canonical == "temperature": self._handle_temperature_command(cmd_original)` |
| `gateway/slash_commands.py` | `_handle_temperature_command()` — Discord handler (mirror `_handle_reasoning_command`) |
| `gateway/run.py` | Dispatch branch + `_session_temperature_overrides` plumbing + wire into agent `request_overrides` |

---

## 3. The Command Semantics

```
/temperature                    → show current (session override or global default)
/temperature 0.7                → set for this session only
/temperature 0.7 --global        → persist to config.yaml
/temperature reset              → clear session override (back to global)
```

Validation: float, 0.0–2.0 (Ollama/OpenAI range). Invalid → friendly error, no state change.

---

## 4. The Session Override Plumbing (the part the Google doc missed)

Mirror the `/reasoning` pattern in `gateway/run.py`:

```python
# In _CONVERSATION_SCOPED_STATE tuple (line ~2575):
"_session_temperature_overrides",

# Property (next to _session_reasoning_overrides, line ~5933):
_session_temperature_overrides = legacy_dict_property("_session_temperature_overrides")

# Setter (mirror _set_session_reasoning_override, line ~8419):
def _set_session_temperature_override(self, session_key, temperature):
    if not session_key:
        return
    self._session_state(session_key).conversation.temperature_override = temperature

# Resolver (mirror _resolve_session_reasoning_config, line ~8390):
def _resolve_session_temperature(self, *, source=None, session_key=None):
    # session override > config default
```

**The critical wiring — where this connects to our API patch:**

In `_create_agent()` (api_server.py) or the gateway's agent-creation path, merge the session temperature into `request_overrides`:

```python
if session_temperature is not None:
    agent_kwargs["request_overrides"] = {
        **dict(agent_kwargs.get("request_overrides") or {}),
        "temperature": session_temperature,
    }
```

This is the same `request_overrides` channel our `model_options.generation` patch uses — so the slash command and the API both feed the same mechanism. The Modulator can set temperature programmatically via the API; a human can set it interactively via `/temperature`. Same channel, two doors.

**Agent eviction:** after setting, call `self._evict_cached_agent(session_key)` so the next turn picks up the new temperature (mirror `/reasoning` line ~3403).

---

## 5. CLI Handler (TUI/dashboard) — mirror `_handle_reasoning_command`

In `hermes_cli/cli_commands_mixin.py`:

```python
def _handle_temperature_command(self, cmd: str):
    """Handle /temperature — view or set LLM sampling temperature.

    Usage:
        /temperature              Show current temperature
        /temperature <0.0-2.0>    Set for this session only
        /temperature <0.0-2.0> --global  Persist to config.yaml
        /temperature reset        Clear session override
    """
    args = cmd.split()[1:] if cmd.strip() else []
    if not args:
        # show current: session override or config default
        ...
        return
    if args[0] == "reset":
        # clear session override
        ...
        return
    persist_global = "--global" in args
    value = [a for a in args if a != "--global"][0]
    try:
        new_temp = float(value)
        if not (0.0 <= new_temp <= 2.0):
            raise ValueError()
    except (ValueError, IndexError):
        _cprint(f"  {_Colors.ERROR}✗ Temperature must be a number between 0.0 and 2.0.{_Colors.RESET}")
        return
    if persist_global:
        # write to config.yaml (mirror _save_gateway_config_key pattern)
        ...
    else:
        # set session override
        ...
    _cprint(f"  {_Colors.SUCCESS}✓ Generation temperature set to {new_temp}{_Colors.RESET}")
```

---

## 6. Gateway Handler (Discord) — mirror `_handle_reasoning_command`

In `gateway/slash_commands.py`:

```python
async def _handle_temperature_command(self, event: MessageEvent) -> Optional[str]:
    """Handle /temperature — view or set LLM sampling temperature.

    Usage:
        /temperature                   Show current temperature
        /temperature <0.0-2.0>         Set for this session only
        /temperature <0.0-2.0> --global  Persist to config.yaml
        /temperature reset             Clear session override
    """
    raw_args = event.get_command_args().strip()
    # normalize source → session_key (mirror /reasoning line ~3508)
    _temp_source = await asyncio.to_thread(self._normalize_source_for_session_key, event.source)
    session_key = self._session_key_for_source(_temp_source)

    if not raw_args:
        # show current state (session override or global default)
        return f"Current temperature: {current}"

    # parse args, validate 0.0-2.0, handle --global / reset
    # set session override or persist to config
    # evict cached agent
    return f"Generation temperature set to {new_temp}"
```

---

## 7. Registry Entry

In `hermes_cli/commands.py`, next to `/reasoning` (line ~234):

```python
CommandDef("temperature", "View or set LLM sampling temperature (session-scoped; --global to persist)",
           "Configuration", aliases=("temp",),
           args_hint="[0.0-2.0] [--global] [reset]",
           subcommands=("reset", "--global")),
```

---

## 8. Testing

1. **Unit:** registry resolves `temperature` + `temp` alias; arg parsing/validation (0.0–2.0, non-numeric, out-of-range, `--global`, `reset`).
2. **CLI:** `_handle_temperature_command` — show/set/global/reset paths (mirror `tests/cli/test_*_command.py` patterns).
3. **Gateway:** `_handle_temperature_command` — session override set/clear, agent eviction called (mirror `tests/gateway/test_discord_slash_commands.py` patterns).
4. **Integration:** set `/temperature 0.7` in a session → next turn's agent has `request_overrides["temperature"] == 0.7` → flows to the API call (this is the link to our `model_options.generation` patch).

---

## 9. Why This Makes the PR Stronger

- **Complete feature:** API + CLI + Discord UI, all in one PR. The maintainers click "Approve" and get a shippable, advertisable feature — not a bare API hook.
- **Follows the house pattern:** `/reasoning` is the template; reviewers see familiar structure.
- **Backward compatible:** absent `generation` / no `/temperature` set → zero behavior change.
- **Real utility:** anyone can tune sampling params interactively on any surface, not just via API.

---

## 10. Scope Decision (for Rob)

The Google doc suggested a single `/temperature` command. Options:
- **A. `/temperature` only** — smallest, covers the Modulator's primary output (temperature). ~5 files, mirrors `/reasoning` exactly.
- **B. `/temperature` + `/top_p` + `/max_tokens`** — fuller "customizable params" story for the PR, but 3× the handlers. Could be one `/set` command with subcommands (the doc's "unified command" tip).

**Recommendation: A first** (ship `/temperature`, prove the pattern), then extend to B if the maintainers want it. The PR stays small and reviewable.

---

*Draft for Rob's review — not yet applied. The API patch is already implemented and tested; this adds the interactive surface on top.*
