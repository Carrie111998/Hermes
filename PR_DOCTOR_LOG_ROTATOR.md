# Upstream PR Description: Add 'hermes doctor logs' One-Shot Log Rotator

**Branch**: `contrib/doctor-log-rotator`  
**Base**: `main` (`b39d76d902b0891457bc73a6eb43aa136f26d7a8`)  
**Type**: Feature / Diagnostics

---

## Summary
Adds `hermes doctor logs <path>` for on-demand rename-rotation of foreign log files (e.g. `server.log`, `dashboard.error.log`) without hand-truncating active logs.

### Features
- One-shot rename-rotation of single log files, shifting `.1 -> .2 -> ...` up to `--backups` (default 3).
- Caps inferred from file path (10 MiB for oMLX server logs, 5 MiB for error logs) with `--max-bytes` override.
- Writer-aware oMLX rotation: for app-managed `server.log`, `--reopen-command` is required and verified via `lsof` to ensure the new path gains an active writer before reporting success.

## Tests Added
- `tests/hermes_cli/test_doctor_logs.py`: 9 unit and integration tests covering argument parsing, forced rotation, no-op threshold handling, and held-open FD writer handoff verification.
