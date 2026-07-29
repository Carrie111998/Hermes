# AIRI ↔ Hermes Agent process worker

This plugin runs [Project AIRI](https://github.com/zapabob/airi) as a **Hermes-managed Electron process worker** that can sit **side by side with Hermes Desktop**:

| Surface | Role | Electron? |
|---------|------|-----------|
| Hermes gateway **api_server** (`:8642/v1/`) | Shared OpenAI-compatible AI core | No |
| Hermes Desktop (`Hermes.exe`, app id `com.nousresearch.hermes`) | Chat / sessions UI | Yes |
| AIRI stage-tamagotchi (app id `ai.moeru.airi`) | VRM / TTS / companion shell | Yes |
| This plugin | Worker supervisor + auth sync + CDP seed + local OSC | — |

**Concurrent by design.** `hermes airi start` does **not** stop Hermes Desktop (and Desktop does not stop AIRI). Isolation:

- `APP_USER_DATA_PATH=~/.hermes/airi/userdata` (Electron single-instance lock is per userData)
- Distinct AppUserModelId: `ai.moeru.airi` vs `com.nousresearch.hermes`
- CDP debug port **9455** (avoids Desktop perf `:9222` / `:9333`)
- Worker state: `~/.hermes/airi/worker-state.json`

## Setup

```bash
git submodule update --init --recursive vendor/airi
cd vendor/airi
pnpm install
```

Ensure the Hermes **gateway OpenAI API** is up (`API_SERVER_KEY` in `~/.hermes/.env`, default `:8642/v1/`). Desktop `:9119` is the session backend — AIRI talks to the OpenAI-compatible `api_server`.

## Sync + start (safe beside Desktop)

```bash
hermes airi sync          # provider template + probe + auth readiness
hermes airi start         # sync → start AIRI Electron worker → CDP seed+reload
hermes airi status        # worker health + auth (no secrets echoed)
hermes airi restart       # AIRI only — Desktop stays up
hermes airi stop          # AIRI only — never kills Hermes.exe
```

Equivalent agent tools: `airi_sync`, `airi_start`, `airi_restart`, `airi_status`, `airi_stop`.

`airi_start` will:

1. Repair AIRI tray/window icon if `resources/icon.png` is an indexed (palette) PNG (Windows Electron can colour-invert those).
2. Write `~/.hermes/airi/hermes-provider.json` (baseUrl **with trailing slash**; raw key not persisted).
3. Probe `GET {baseUrl}models` with Bearer `API_SERVER_KEY` and report `auth.ready`.
4. Launch `pnpm dev:tamagotchi` with isolated userData, `--remote-debugging-port=9455`, `--remote-allow-origins=*`.
5. Seed AIRI renderer localStorage (credentials + consciousness) via CDP, then **reload** so Pinia picks up the keys.

CDP seeding needs `websocket-client` in the Hermes venv (`uv pip install websocket-client`).

Secrets in `~/.hermes/.env` only:

```text
API_SERVER_KEY=<gateway openai bearer>
# optional override:
# HERMES_AIRI_API_KEY=<same or alternate token>
```

## VRChat

VRChat OSC must be enabled in VRChat. Defaults to `127.0.0.1:9000`:

- `airi_vrchat_chatbox`
- `airi_vrchat_parameter`
- `airi_vrchat_autonomy`

No hidden background control loop. Avatar actions stay explicit and local.

## Limitations

- AIRI does not natively read Hermes env for chat credentials; this plugin seeds localStorage over CDP after launch (plus reload).
- If CDP seeding fails (Electron not ready / port busy), re-run `hermes airi start` or `hermes airi sync` while the worker is up.
- AIRI's own `requestSingleInstanceLock` still prevents two AIRI copies; that does **not** conflict with Hermes Desktop.
