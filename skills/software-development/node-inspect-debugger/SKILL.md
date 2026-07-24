---
name: node-inspect-debugger
description: "Debug Node.js via --inspect + Chrome DevTools Protocol CLI."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [debugging, nodejs, node-inspect, cdp, breakpoints, ui-tui]
    related_skills: [systematic-debugging, python-debugpy, debugging-hermes-tui-commands]
---

# Node.js Inspect Debugger

## Overview

When `console.log` isn't enough, drive Node's built-in V8 inspector programmatically from the terminal. You get real breakpoints, step in/over/out, call-stack walking, local/closure scope dumps, and arbitrary expression evaluation in the paused frame.

Two tools, pick one:

- **`node inspect`** — built-in, zero install, CLI REPL. Best for quick poking.
- **`ndb` / CDP via `chrome-remote-interface`** — scriptable from Node/Python; best when you want to automate many breakpoints, collect state across runs, or debug non-interactively from an agent loop.

**Prefer `node inspect` first.** It's always available and the REPL is fast.

## When to Use

- A Node test fails and you need to see intermediate state
- ui-tui crashes or behaves wrong and you want to inspect React/Ink state pre-render
- tui_gateway child processes (`_SlashWorker`, PTY bridge workers) misbehave
- You need to inspect a value in a closure that `console.log` can't reach without patching
- Perf: attach to a running process to capture a CPU profile or heap snapshot

**Don't use for:** things `console.log` solves in under a minute. Breakpoint-driven debugging is heavier; use it when the payoff is real.

## Reference Map

| To do this | Read |
|---|---|
| Look up a `debug>` command (breakpoints, stepping, `bt`, `repl`, `watch`) | `references/node-inspect-command-reference.md` |
| Attach to a running PID, choose `--inspect` flags/ports, debug TS via tsx, run Vitest under the debugger | `references/attach-and-launch-configs.md` |
| Script CDP with `chrome-remote-interface`; dump scopes; take CPU profiles / heap snapshots | `references/cdp-scripting.md` |
| Debug the Hermes Ink TUI or a running `hermes --tui` | `references/hermes-ui-tui-recipes.md` |
| Breakpoint not hitting, wrong TS line numbers, port collisions, child processes, PTY issues | `references/pitfalls-and-troubleshooting.md` |
| Copy a ready-made recipe for a common symptom | `references/one-shot-recipes.md` |

## End-to-End Skeleton

```bash
node --inspect-brk path/to/script.js &   # launch paused on first line
node inspect -p $!                        # attach the CLI REPL
```

```
debug> sb('script.js', 42)   # set the breakpoint BEFORE any code runs
debug> cont                  # run to it
debug> bt                    # who called us
debug> repl                  # inspect locals/closures in the paused frame
> myVariable
debug> cont                  # release the target before you detach
```

## Non-Negotiables

- **Bind the inspector to `127.0.0.1` only.** An inspector port is arbitrary remote code execution;
  `--inspect=0.0.0.0` exposes it to the network.
- **Use `--inspect-brk`, not `--inspect`, when you need breakpoints set before code runs.** Plain
  `--inspect` lets the script race past your breakpoint while you are still attaching.
- **Debug one worker at a time.** `--no-file-parallelism` (vitest) / `--runInBand` (jest); a pool is
  not debuggable.
- **Never leave a target paused.** `cont` or `kill` before detaching, or the process hangs forever.
- **Verify you attached to the process you meant.** Confirm the target before drawing any conclusion
  from what you see.
- **Python children are out of scope** — `_SlashWorker` and PTY workers belong to `python-debugpy`.

## Verification Checklist

After setting up a debug session, verify:

- [ ] `curl -s http://127.0.0.1:9229/json/list` returns exactly the target you expect
- [ ] First breakpoint actually hits (if it doesn't, you likely missed `--inspect-brk` or attached after execution completed)
- [ ] Source listing at pause shows the right file (mismatch = sourcemap issue, see pitfall 1 in `references/pitfalls-and-troubleshooting.md`)
- [ ] `exec process.pid` in `repl` returns the PID you meant to attach to
