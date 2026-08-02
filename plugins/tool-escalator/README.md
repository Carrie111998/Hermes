# tool-escalator

Auto-escalate to MoA (Mixture of Agents) when consecutive tool errors are detected.

## How it works

The plugin watches three lifecycle hooks to detect, escalate, and de-escalate:

```
T1 (your daily model, unchanged)
└─ on N (default 3) consecutive tool errors →
T2 (MoA preset — user selects manually via /moa or /model)
└─ after MoA aggregation completes → auto-de-escalate logging
```

### Three hook callbacks

| Hook | What it does |
|------|-------------|
| `post_tool_call` | Checks every tool result for error indicators (`"error"`, `"failed"`, `Error:`-prefixed lines, non-zero exit codes). Increments a session-scoped consecutive-error counter on failure; resets to zero on success. At threshold (default 3), logs an escalation decision and sets a session flag. |
| `pre_llm_call` | Checks the escalation flag and injects context into the user message when escalation is active, nudging the model or user to switch to MoA. Also detects ongoing MoA calls for de-escalation tracking. |
| `post_llm_call` | Detects MoA completion (via the pre_llm_call MoA-active marker) and clears the escalation flag, logging the de-escalation. |

### Error detection

Pragmatic pattern matching on the string representation of every tool result:

- Substring match for `"error"`, `"failed"`, `"failure"`, `"exception"`, `"traceback"`, `"timeout"`
- `Error:` / `ERROR`-prefixed lines
- Non-zero exit code patterns (`exit code #` where # ≠ 0)

## Configuration

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `plugins.entries.tool-escalator.config.threshold` | int | 3 | Consecutive tool errors before escalation |

Example `config.yaml`:

```yaml
plugins:
  entries:
    tool-escalator:
      enabled: true
      config:
        threshold: 5
```

## Hooks declared

- `post_tool_call`
- `pre_llm_call`
- `post_llm_call`
