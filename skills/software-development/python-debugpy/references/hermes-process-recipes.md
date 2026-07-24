# Debugging Hermes-specific Python Processes

## Tests

See `local-pdb-recipes.md` Recipe 3. Always add `-p no:xdist` or run single tests without xdist.

## `run_agent.py` / CLI — one-shot

Easiest: add `breakpoint()` near the suspect line, then run `hermes` normally. Control returns to
your terminal at the pause point.

## `tui_gateway` subprocess (spawned by `hermes --tui`)

The gateway runs as a child of the Node TUI. Options:

**A. Source-edit the gateway:**
```python
# tui_gateway/server.py near the top of serve()
import debugpy
debugpy.listen(("127.0.0.1", 5678))
debugpy.wait_for_client()
```
Start `hermes --tui`. The TUI will appear frozen (its backend is waiting). Attach a client;
execution resumes when you `continue`.

**B. Use `remote-pdb` at a specific handler:**
```python
from remote_pdb import set_trace
set_trace(host="127.0.0.1", port=4444)   # in the RPC handler you want to trap
```
Trigger the matching slash command from the TUI, then `nc 127.0.0.1 4444` in another terminal.

## `_SlashWorker` subprocess

Same pattern — `remote-pdb` with `set_trace()` inside the worker's `exec` path. The worker is
persistent across slash commands, so the first trigger blocks until you connect; subsequent slash
commands pass through normally unless you re-arm.

## Gateway (`gateway/run.py`)

Long-lived. Use `remote-pdb` at a handler, or `debugpy` with `--wait-for-client` if you're
restarting the gateway anyway.
