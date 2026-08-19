# Hermes Desktop ☤

<p align="center">
  <a href="https://github.com/NousResearch/hermes-agent/releases"><img src="https://img.shields.io/badge/Download-macOS%20%C2%B7%20Windows%20%C2%B7%20Linux-FFD700?style=for-the-badge" alt="Download"></a>
  <a href="https://hermes-agent.nousresearch.com/docs/"><img src="https://img.shields.io/badge/Docs-hermes--agent.nousresearch.com-FFD700?style=for-the-badge" alt="Documentation"></a>
  <a href="https://discord.gg/NousResearch"><img src="https://img.shields.io/badge/Discord-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://github.com/NousResearch/hermes-agent/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License: MIT"></a>
</p>

**The native desktop app for [Hermes Agent](../../README.md) — the self-improving AI agent from [Nous Research](https://nousresearch.com).** Same agent, same skills, same memory as the CLI and gateway, in a polished native window — chat with streaming tool output, side-by-side previews, a file browser, voice, and settings, no terminal required. Available for **macOS, Windows, and Linux**.

<table>
<tr><td><b>Chat with the full agent</b></td><td>Streaming responses, live tool activity, structured tool summaries, and the same conversation history as every other Hermes surface.</td></tr>
<tr><td><b>Side-by-side previews</b></td><td>Render web pages, files, and tool outputs in a right-hand pane while you keep chatting.</td></tr>
<tr><td><b>File browser</b></td><td>Explore and preview the working directory without leaving the app.</td></tr>
<tr><td><b>Voice</b></td><td>Talk to Hermes and hear it back.</td></tr>
<tr><td><b>Settings & onboarding</b></td><td>Manage providers, models, tools, and credentials from a real UI. First-run setup gets you to your first message in seconds.</td></tr>
<tr><td><b>Stays current</b></td><td>Built-in updates pull the latest agent and rebuild the app in place.</td></tr>
</table>

---

## Install

### Install with Hermes (recommended)

Already have the Hermes CLI? Just run:

```bash
hermes desktop
```

It builds and launches the GUI against your existing install — same config, keys, sessions, and skills. On first launch Hermes walks you through picking a provider and model; nothing else to configure.

### Prebuilt installers

Prebuilt installers are built and distributed via [the Hermes Desktop website.](https://hermes-agent.nousresearch.com/).

---

## Updating

The app checks for updates in the background and offers a one-click update when one is ready. You can also update any time from the CLI:

```bash
hermes update
```

IX Agency builds auto-update from the AWS S3 feed (`https://intelliverse-x-desktop.s3.amazonaws.com/ix-agency`): the app polls on launch and every 4 hours, and the **Update available** button installs in place. Releasing a new version is just a version bump merged to `main` — see [RELEASING.md](./RELEASING.md) for the full pipeline.

---

## Requirements

The installer handles everything for you (Python 3.11+, a portable Git, ripgrep).

---

## Development

Want to hack on the app itself? Install workspace deps from the repo root once, then run the dev server from this directory:

```bash
npm install          # from repo root — links apps/desktop, web, apps/shared
cd apps/desktop
npm run dev          # Vite renderer + Electron, which boots the Python backend
```

Point the app at a specific source checkout, or sandbox it away from your real config:

```bash
# throwaway HERMES_HOME, separate Electron userData, distinct app name to avoid the single-instance lock
../scripts/dev-sandbox.sh npm run dev
HERMES_DESKTOP_HERMES_ROOT=/path/to/clone npm run dev
HERMES_HOME=/tmp/throwaway npm run dev
npm run dev:fake-boot   # exercise the startup overlay with deterministic delays
```

### Building installers

```bash
npm run dist:mac     # DMG + zip
npm run dist:win     # NSIS + MSI
npm run dist:linux   # AppImage + deb + rpm
npm run pack         # unpacked app under release/ (no installer)
```

Installers are built and uploaded to GitHub Releases manually. macOS/Windows signing & notarization happen automatically when the relevant credentials are present in the environment (`CSC_LINK` / `CSC_KEY_PASSWORD` / `APPLE_*` for macOS, `WIN_CSC_*` for Windows).

### How it works

The packaged app ships the Electron shell and a native React chat surface. On
first launch it can install the Hermes Agent runtime into `HERMES_HOME`
(`~/.hermes`, or `%LOCALAPPDATA%\hermes` on Windows), using the same layout as a
CLI install.

The app has three boundaries:

- **Electron** resolves and validates a runnable backend, owns native
  filesystem/git/window capabilities, and exposes a narrow preload bridge.
- **React** owns the Desktop routes, panes, interaction state, and
  `@assistant-ui/react` transcript.
- **Hermes Agent** runs as a headless `hermes serve` process and exposes the
  `tui_gateway` JSON-RPC/WebSocket API. The renderer connects through
  [`apps/shared`](../shared/), which is also used by the browser dashboard.

Backend resolution is an ordered ladder:

1. `HERMES_DESKTOP_HERMES_ROOT`
2. the current source checkout during development
3. a completed managed install
4. `HERMES_DESKTOP_HERMES`, or `hermes` on `PATH`
5. a system Python that can import the Hermes runtime
6. the first-launch bootstrap installer

Candidates are probed before use; an existing shim or interpreter is not enough.
A runtime that predates `serve` falls back to headless
`dashboard --no-open`. This is compatibility for the backend command only and
does not launch or embed the dashboard UI.

The Electron orchestration entry point is `electron/main.ts`; pure resolution,
probe, hardening, and platform policies live in focused modules beside it. The
renderer is under `src/`, with shared atoms in `src/store` and transport/native
adapters in `src/lib`.

Before changing the app, read:

- [`AGENTS.md`](./AGENTS.md): architecture, state ownership, resolver/fallback,
  transport, performance, and testing rules.
- [`DESIGN.md`](./DESIGN.md): visual system, information architecture, motion,
  direct manipulation, and keyboard behavior.

### Connections, projects, and switching

Desktop supports a managed local backend, explicit remote gateways, and Hermes
Cloud connections. Remote and cloud modes use the same remote-capability path;
authentication and discovery differ, not the renderer feature model.

Projects are the workspace abstraction. A project may own multiple folders,
repositories, worktrees, and sessions; a bare new chat remains detached unless
the user enters a project or configures a default project directory. Use the
Projects UI rather than adding a second per-session folder-picker workflow.

Changing profiles or connection modes is a soft workspace switch, not another
cold boot. The shell and current management overlay remain mounted while
gateway-bound nanostores are wiped, query-backed data is invalidated, and the
new connection repopulates skeletons. This prevents rows or transcripts from
the previous gateway bleeding into the next one.

### Verification

Run before opening a PR (lint may surface pre-existing warnings but must exit cleanly):

```bash
npm run fix
npm run typecheck
npm run lint
npm run test:ui
npm run test:desktop:platforms
```

Run `npm run test:desktop:all` for install, boot, update, packaging, or other
release-path changes.

### Troubleshooting

Boot logs land in `HERMES_HOME/logs/desktop.log` (includes backend output and recent Python tracebacks) — check it first if the app reports a boot failure.

**macOS / Linux:**

```bash
# Force a clean first-launch setup
rm "$HOME/.hermes/hermes-agent/.hermes-bootstrap-complete"
# Rebuild a broken Python venv
rm -rf "$HOME/.hermes/hermes-agent/venv"
# Reset a stuck macOS microphone prompt (macOS only)
tccutil reset Microphone ai.intelli-verse-x.ix-agency
```

**Windows (PowerShell):**

```powershell
# Force a clean first-launch setup
Remove-Item "$env:LOCALAPPDATA\hermes\hermes-agent\.hermes-bootstrap-complete"
# Rebuild a broken Python venv
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\hermes\hermes-agent\venv"
```

> The default Hermes home on Windows is `%LOCALAPPDATA%\hermes`. Set the `HERMES_HOME` env var if you've relocated it.

---

## IX Agency integration

The `/ix-agency` view adds the company surfaces on top of the stock desktop app
(nothing existing is removed). Everything below is configured in **IX Agency →
Connect**; all secrets are encrypted at rest with Electron `safeStorage`
(Keychain-backed on macOS).

| Setting | Default | Used by |
|---|---|---|
| Portal URL | `https://admin.intelli-verse-x.ai` | Portal tab webview + OTP login gate |
| admin-mcp gateway URL + bearer token | `https://admin-mcp.intelli-verse-x.ai/` | Org tools directory, MCP status lamp, native copilot tool loop |
| LiteLLM base URL + API key | `https://litellm.intelli-verse-x.ai` | Native copilot (Copilot tab) |
| WireGuard `usa-vpn.conf` | imported once via file picker → keychain | VPN connect button + status lamp |
| Expected VPN egress IP | `18.209.130.93` | Lamp turns green only when exit-IP check matches |
| Update feed URL | `https://intelliverse-x-desktop.s3.amazonaws.com/ix-agency` | Update poller (launch + every 4 h) — electron-updater channel `.yml` feed |
| Cognito S2S client id/secret | pool client `7i9clgl5c6dv2qk755ssrrlo80` (secret user-supplied) | Hermes first-run init |

Notes:

- **Login is enforced**: the native copilot and MCP directory require a live
  portal OTP session (probed main-process against the `persist:ix-agency-portal`
  webview session). LiteLLM/gateway credentials alone do not unlock them.
- **Write gate**: any mutating MCP tool call stops with a Confirm card in the
  chat; the confirmation nonce lives only in the main process and execution
  uses the arguments frozen at request time.
- **Updates**: CI publishes the electron-updater feed (`latest.yml` /
  `latest-mac.yml` / `latest-linux.yml` + artifacts) to the S3 feed base on a
  version bump merged to `main` (see [RELEASING.md](./RELEASING.md)). When a
  newer version is available, a non-blocking "Update available — Restart to
  update" button appears in the IX strip and the tray; clicking installs in
  place (Windows NSIS / Linux AppImage / signed macOS), otherwise opens the
  download
  for this platform.
- **VPN status truthfulness**: green requires the tunnel artifact, a fresh
  `wg show` handshake when readable (root-only on macOS — unreadable is
  reported, not faked) and the exit-IP check to match the expected egress IP.
- **Hermes init**: validates the Cognito client-credentials grant (JWKS
  signature verification), then runs `hermes-deployment/scripts/install-local.sh`
  when a checkout exists at `~/dev/hermes-deployment`, else writes a minimal
  `~/.hermes/config.yaml` pointing at the LiteLLM gateway; gateway/LiteLLM
  secrets land in `~/.hermes/.env` (0600).

---

## Community

- 💬 [Discord](https://discord.gg/NousResearch)
- 📖 [Documentation](https://hermes-agent.nousresearch.com/docs/)
- 🐛 [Issues](https://github.com/NousResearch/hermes-agent/issues)

---

## License

MIT — see [LICENSE](../../LICENSE).

Built by [Nous Research](https://nousresearch.com).
