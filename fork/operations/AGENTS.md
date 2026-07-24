# Operations — Agent Instructions

## Restart order (llama excluded)

1. Stop desktop Electron processes if rebuilding UI.
2. `scripts\windows\restart-hermes-stack.ps1` (no `-StartLlama` unless recovery).
3. Fix venv if desktop backend fails: `pip install -e ".[web]"` in `.venv`.
4. `hermes desktop --build-only --force-build` then launch packaged `Hermes.exe`.

## Port reference

| Port | Service |
|------|---------|
| 8787 | Hermes-WebUI (`HERMES_WEBUI_PORT` / `start-hermes-webui.ps1`) — **not** the messaging gateway |
| 9118 | Go watchdog managed prewarm `hermes serve` (not Desktop's default) |
| 9119 | Desktop / `hermes serve` (headless backend) |
| 9120 | `hermes dashboard` |
| 9920 | Go watchdog HTTP control plane (`127.0.0.1` only) |
| 8080 / 8081 | llama.cpp / proxy (optional; restart only with `-StartLlama`) |
| 8646 | LINE ngrok/webhook helper |
| 3001 | FreeLLMAPI local proxy |

Messaging gateway (`start-hermes-gateway.ps1` / `hermes gateway`) is a **separate process** and does **not** bind to 8787. Check liveness with `hermes gateway status`, not by probing `:8787`.

## Go watchdog restart notes

- `restart-hermes-stack.ps1 -StartGoWatchdog` reuses `watchdog-go/dist/hermes-watchdog.exe` when present (no rebuild).
- Missing exe → bounded `BuildIfMissing` (SkipTest, 180s timeout); failure skips watchdog instead of hanging the stack.
- Managed serve is launched with `--skip-build`; prewarm runs asynchronously so the control plane is not blocked.

## Tailscale

Run `tailscale up` if tailnet IP missing. Refresh Serve after stack restart when llama routes change.

## Do not commit

- `~/.hermes/` runtime state
- `apps/desktop/release/`, `dist/`, `node_modules/`
- Implementation logs in `_docs/`
- One-off probes under `tmp/probes/`; keep them out of the source tree

## Logs

- `~/.hermes/logs/agent.log`, `gateway.log`, `desktop.log`
- `hermes logs --follow`

## When gateway locks files

`uv sync` may fail on `PyNaCl` DLL locks — stop gateway first, or use `pip install -e ".[web]"` for quick dep repair.
