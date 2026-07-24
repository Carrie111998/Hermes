# Debugging Hermes ui-tui

The TUI is built Ink + tsx. Two common scenarios.

## Debugging a single Ink component under dev

`ui-tui/package.json` has `npm run dev` (tsx --watch). Add `--inspect-brk` by running tsx directly:

```bash
cd "$(git rev-parse --show-toplevel)/ui-tui"
npm run build    # produce dist/ once so transpile isn't needed on first load
node --inspect-brk dist/entry.js
# In another terminal:
node inspect -p <node pid>
```

Then inside `debug>`:

```
sb('dist/app.js', 220)     # or wherever the suspect render is
cont
```

When it pauses, `repl` → inspect `props`, state refs, `useInput` handler values, etc.

## Debugging a running `hermes --tui`

The TUI spawns Node from the Python CLI. Easiest path:

```bash
# 1. Launch TUI
hermes --tui &
TUI_PID=$(pgrep -f 'ui-tui/dist/entry' | head -1)

# 2. Enable inspector on that Node PID
kill -SIGUSR1 "$TUI_PID"

# 3. Find the WS URL
curl -s http://127.0.0.1:9229/json/list | jq -r '.[0].webSocketDebuggerUrl'

# 4. Attach
node inspect ws://127.0.0.1:9229/<uuid>
```

Interacting with the TUI (typing in its window) continues to advance execution; your debugger can
pause it on a breakpoint at any `sb(...)`.

## `_SlashWorker` / PTY child processes

Those are Python, not Node — use the `python-debugpy` skill for them. Only Node portions (Ink UI,
tui_gateway client, tsx-run tests under `ui-tui/`) use this skill.
