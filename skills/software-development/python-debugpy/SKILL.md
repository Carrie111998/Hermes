---
name: python-debugpy
description: "Debug Python: pdb REPL + debugpy remote (DAP)."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [debugging, python, pdb, debugpy, breakpoints, dap, post-mortem]
    related_skills: [systematic-debugging, node-inspect-debugger, debugging-hermes-tui-commands]
---

# Python Debugger (pdb + debugpy)

## Overview

Three tools, picked by situation:

| Tool | When |
|---|---|
| **`breakpoint()` + pdb** | Local, interactive, simplest. Add `breakpoint()` in the source, run normally, get a REPL at that line. |
| **`python -m pdb`** | Launch an existing script under pdb with no source edits. Useful for quick poking. |
| **`debugpy`** | Remote / headless / "attach to already-running process." Talks DAP, scriptable from terminal, works for long-lived processes (gateway, daemon, PTY children). |

**Start with `breakpoint()`.** It's the cheapest thing that works.

## When to Use

- A test fails and the traceback doesn't reveal why a value is wrong
- You need to step through a function and watch a collection mutate
- A long-running process (hermes gateway, tui_gateway) misbehaves and you can't restart it
- Post-mortem: an exception fired in prod-ish code and you want to inspect locals at the crash site
- A subprocess / child (Python `_SlashWorker`, PTY bridge worker) is the actual bug site

**Don't use for:** things `print()` / `logging.debug` solve in under a minute, or things
`pytest -vv --tb=long --showlocals` already reveals.

## Reference Map

| To do this | Read |
|---|---|
| Look up a pdb command (step, breakpoint, stack, `interact`) | `references/pdb-command-reference.md` |
| Local `breakpoint()`, `python -m pdb`, pytest `--pdb`/`--trace`, post-mortem | `references/local-pdb-recipes.md` |
| Remote debug a running process: `debugpy` listen/attach patterns, DAP client script, VS Code `launch.json`, `remote-pdb` | `references/debugpy-remote-and-ide-setup.md` |
| Debug Hermes CLI, `tui_gateway`, `_SlashWorker`, or the gateway | `references/hermes-process-recipes.md` |
| Breakpoint not hitting, xdist/threads/asyncio/fork problems, ptrace denied | `references/pitfalls-and-troubleshooting.md` |
| Copy a ready-made recipe for a common symptom | `references/one-shot-recipes.md` |

## End-to-End Skeleton

Local code you can edit and re-run:

```python
def compute(x, y):
    result = some_helper(x)
    breakpoint()           # pdb REPL lands here
    return result + y
```

```
(Pdb) pp result      # inspect
(Pdb) w              # how did we get here
(Pdb) c              # continue
```

Long-lived process you cannot restart clean:

```python
from remote_pdb import set_trace
set_trace(host="127.0.0.1", port=4444)   # blocks until you connect
```

```bash
nc 127.0.0.1 4444    # same (Pdb) prompt, from another terminal
```

## Non-Negotiables

- **Never commit a debug hook.** No `breakpoint()`, `set_trace()`, or `debugpy.listen` in committed
  code — in CI / non-TTY contexts they hang the process.
- **pdb does not work under pytest-xdist.** Always add `-p no:xdist` or `-n 0`; otherwise the test
  just hangs with no prompt.
- **Bind debug listeners to `127.0.0.1` only.** A debug port is arbitrary remote code execution.
- **Reproduce under the real wrapper before you claim a fix.** Raw `pytest` bypasses the hermetic
  env (`scripts/run_tests.sh` strips credentials and moves `HOME`), so a repro there is not proof.
- **Debug one process at a time.** pdb does not follow forks or other threads.

## Verification Checklist

- [ ] After `pip install debugpy`, confirm: `python -c "import debugpy; print(debugpy.__version__)"`
- [ ] For remote debug, confirm the port is actually listening: `ss -tlnp | grep 5678`
- [ ] First breakpoint actually hits (if it doesn't, you likely have `PYTHONBREAKPOINT=0`, you're under xdist, or execution finished before attach)
- [ ] `where` / `w` shows the expected call stack
- [ ] Post-debug cleanup: no stray `breakpoint()` / `set_trace()` in committed code
  ```bash
  rg -n 'breakpoint\(\)|set_trace\(|debugpy\.listen' --type py
  ```
