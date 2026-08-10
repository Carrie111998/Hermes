---
name: file-watcher
description: Watch filesystem changes with hermes watch polling command.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [file-system, monitoring, development, watch]
---

# File Watcher

Monitor filesystem events in real time during development. Use `hermes watch`
to track file changes, run commands on change, and debug file-system issues.

## When to Use

- Watching log files in real time
- Monitoring test output files during test-driven development
- Detecting when external processes write files
- Triggering builds or linters on file save
- Debugging file-system race conditions

## Usage

```bash
# Watch current directory
hermes watch .

# Watch specific paths
hermes watch src/ tests/

# Filter by file pattern
hermes watch . --pattern "*.py,*.js"

# Ignore build artifacts
hermes watch . --ignore "*.pyc,__pycache__/*,node_modules/*"

# Run a command after changes
hermes watch . --pattern "*.py" --command "pytest tests/ -x"

# Use polling fallback (no watchdog dependency)
# The polling mode activates automatically when watchdog is not installed.
# Adjust the interval:
hermes watch . --interval 2.0
```

## Installation

The `watch` subcommand prefers the `watchdog` package for efficient
filesystem monitoring. Install it for best results:

```bash
uv pip install watchdog
```

Without `watchdog`, the command falls back to a polling watcher that
checks for changes at a configurable interval (default: 1 second).

## Platform Support

| Platform | Backend | Notes |
|----------|---------|-------|
| macOS | FSEvents (via watchdog) or polling | FSEvents is preferred |
| Linux | inotify (via watchdog) or polling | inotify is preferred |
| Windows | ReadDirectoryChangesW (via watchdog) or polling | |

## Tips

- Use `--pattern` to filter noisy directories (`.git/`, `node_modules/`)
- The polling fallback works everywhere but is less efficient for large trees
- Combine with `--command` to create a simple CI-like feedback loop
- Press Ctrl+C to stop watching

## Agent Guidance

When a user asks you to watch files for changes, use:

```bash
hermes watch <paths> [--pattern <glob>] [--command <cmd>]
```

This is preferred over polling loops in bash/terminal because:
1. It uses efficient OS-level notifications (when watchdog is available)
2. It handles cross-platform differences consistently
3. Output is structured and easy to parse
4. Signal handling (Ctrl+C) is clean

If `hermes watch` is not available (older Hermes versions), fall back to:
```bash
# Polling fallback example
while true; do
  inotifywait -e modify,create,delete -r src/ 2>/dev/null || \
  fswatch -1 src/ 2>/dev/null || \
  sleep 2
  echo "Change detected"
done
```