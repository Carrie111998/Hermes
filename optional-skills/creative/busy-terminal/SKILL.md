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

- The user asks for a terminal screensaver, an idle animation, or "make this
  terminal look busy"
- The user wants an ambient background pane during a stream, demo, or recording
- The user names it directly ("busy terminal", "fake work screensaver")

Do not reach for this when the user wants real build, test, or git output —
run the real thing with `terminal` instead.

## Prerequisites

Python 3.9+. No third-party packages, no API keys, no network.

Colour needs a terminal that understands ANSI escapes. Output that is piped or
redirected degrades to plain text automatically, as does setting `NO_COLOR`.

## How to Run

Use the `terminal` tool. It runs until Ctrl-C unless given a `--duration`.

```bash
python3 ~/.hermes/skills/creative/busy-terminal/scripts/busy_terminal.py
```

Prefer a bounded run when starting it on the user's behalf, so it cannot
outlive their attention:

```bash
python3 .../busy_terminal.py --duration 300        # five minutes
python3 .../busy_terminal.py --scene tests --speed 2
python3 .../busy_terminal.py --seed 7 --no-color   # reproducible, plain
```

## Quick Reference

| Flag | Default | Effect |
|------|---------|--------|
| `--duration` | `0` | Seconds to run; `0` means until Ctrl-C |
| `--speed` | `1.0` | Time multiplier — `2` is twice as fast |
| `--scene` | cycle | Pin one of `code`, `build`, `tests`, `git` |
| `--seed` | random | Reproducible run |
| `--no-color` | off | Plain text, no ANSI escapes |

| Scene | What it shows |
|-------|---------------|
| `code` | Editor pane, line numbers, source typed and highlighted |
| `build` | Vite / Cargo / Docker output, progress bar, artifact sizes |
| `tests` | Pytest-style dots, pass–fail summary, occasional flake retry |
| `git` | Commit, push with delta compression, CI checks going green |

## Procedure

1. Confirm the terminal is one the user can watch and interrupt — this takes
   over the pane it runs in.
2. Pick a `--duration` unless the user explicitly wants it open-ended.
3. Launch it with `terminal`. Tell the user Ctrl-C stops it.

Scenes never repeat back to back; `next_scene` excludes the one that just
played, so the cycle reads as varied rather than random-looking.

## Pitfalls

- It owns the pane. Start it in a terminal the user is not working in, or they
  will have to Ctrl-C to get their prompt back.
- Backgrounding it (`terminal(background=True)`) is pointless — the output is
  the entire feature and a background process writes it nowhere visible.
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
