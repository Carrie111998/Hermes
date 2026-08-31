---
name: busy-terminal
description: "Joke screensaver faking a live coding session."
version: 1.0.0
author: "Luke The Dev (@iamlukethedev), Hermes Agent"
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [creative, screensaver, terminal, ascii, fun, animation]
    category: creative
---

# Busy Terminal Skill

Fills a terminal with an invented coding session — an editor typing source, a
build, a test run, and git activity — cycling at random until stopped. It is a
screensaver in the `cmatrix` / `genact` tradition.

Nothing it prints is real. It reads no files, runs no commands, and opens no
sockets; every path, SHA, and byte count is generated. It reports no status
about the user's actual work and should never be presented as if it did.

## When to Use

Trigger on any of these, without asking a follow-up question:

- "pretend I'm working" / "make it look like I'm working" / "look busy"
- "start the fake work screensaver" / "busy terminal" / "fake coding"
- The user wants an ambient background pane during a stream, demo, or recording

Do not reach for this when the user wants real build, test, or git output —
run the real thing with `terminal` instead. Never describe its output as if it
reflected real work; it is invented and reports nothing about the repository.

## Prerequisites

Python 3.9+. No third-party packages, no API keys, no network.

Colour needs a terminal that understands ANSI escapes. Output that is piped or
redirected degrades to plain text automatically, as does setting `NO_COLOR`.

## How to Run

Always pass `--window`. Run it through the `terminal` tool:

```bash
python3 ~/.hermes/skills/creative/busy-terminal/scripts/busy_terminal.py \
  --window --duration 600
```

`--window` opens a fresh terminal window on the user's screen, then returns
immediately. Without it the animation writes into the agent's captured pipe,
where there is no TTY to animate and — at the default unbounded duration — the
turn never ends.

Pick a `--duration` so the window cannot outlive the user's attention. Ten
minutes is a good default; they can Ctrl-C sooner.

Variants worth offering:

```bash
... --window --scene tests --speed 2   # one scene, faster
... --window --duration 300            # five minutes
```

The user can also run it themselves in any terminal, without `--window`.

## Quick Reference

| Flag | Default | Effect |
|------|---------|--------|
| `--duration` | `0` | Seconds to run; `0` means until Ctrl-C |
| `--speed` | `1.0` | Time multiplier — `2` is twice as fast |
| `--scene` | cycle | Pin one of `code`, `build`, `tests`, `git` |
| `--seed` | random | Reproducible run |
| `--no-color` | off | Plain text, no ANSI escapes |
| `--window` | off | Open a new terminal window and return — use this from an agent |

| Scene | What it shows |
|-------|---------------|
| `code` | Editor pane, line numbers, source typed and highlighted |
| `build` | Vite / Cargo / Docker output, progress bar, artifact sizes |
| `tests` | Pytest-style dots, pass–fail summary, occasional flake retry |
| `git` | Commit, push with delta compression, CI checks going green |

## Procedure

1. Run the script with `--window` and a `--duration` via `terminal`.
2. Tell the user a new window opened and that Ctrl-C in it stops the show.
3. Suggest full screen and a larger font if they want it to fill the display.

Scenes never repeat back to back; `next_scene` excludes the one that just
played, so the cycle reads as varied rather than random-looking.

## Pitfalls

- **Forgetting `--window` hangs the turn.** The default duration is unbounded,
  so a captured run never returns and the user sees nothing.
- It owns the pane it runs in. `--window` gives it its own, which is why that
  is the agent's path.
- Backgrounding it (`terminal(background=True)`) is not a substitute — the
  output is the entire feature and a background process writes it nowhere
  visible.
- On Linux `--window` needs a terminal emulator on PATH; it raises
  `NoTerminalError` naming the ones it tried. Over plain SSH with no emulator,
  fall back to telling the user to run it without `--window`.
- Under 40 columns the editor pane and artifact table wrap badly. `Console`
  floors the width at 40, but a genuinely tiny terminal still looks cramped.
- `--speed` scales pauses, not content. Very high values (>10) reduce it to a
  wall of text with no rhythm.

## Verification

- Output appears within a second and keeps scrolling
- Over a few minutes all four scenes appear, none twice in a row
- Ctrl-C exits cleanly and the cursor comes back (no invisible prompt)
- `--no-color` output contains no `\033[` sequences
- Two runs with the same `--seed` produce the same transcript
