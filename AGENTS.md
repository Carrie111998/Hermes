# Hermes Agent - Development Guide

Instructions for AI coding assistants and developers working on the hermes-agent codebase.

**Never give up on the right solution.**

> **Long-form guide:** contribution rubric, architecture, tools, config, testing,
> and pitfalls live in
> [`fork/harness/upstream-development-guide.md`](fork/harness/upstream-development-guide.md)
> (upstream-aligned). Fork-owned catalog:
> [Fork-specific features](fork/harness/upstream-development-guide.md#fork-specific-features-for-ai-agents).
> Keep *this* file short for Cursor / continual-learning.

## What Hermes Is

Hermes is a personal AI agent with one core across CLI, messaging gateway
(~20 platforms), TUI, and Electron desktop. Capability grows via **plugins and
skills**, not by thickening the core.

Two sacred properties:

- **Per-conversation prompt caching is sacred.** Do not mutate past context,
  swap toolsets, or rebuild the system prompt mid-conversation (exception:
  context compression).
- **Narrow waist; capability at the edges.** Prefer: extend existing → CLI +
  skill → service-gated tool (`check_fn`) → plugin → MCP catalog → new core
  tool (last resort).

## Fork Overlay

This checkout keeps fork-only behaviour at extension and ops edges. Before an
upstream merge or fork-owned edit, read [`fork/AGENTS.md`](fork/AGENTS.md) and
the nested guide. Authoritative merge mechanism:
`scripts/merge_tools/` / `scripts/sync_all.py` — upstream base, reapply verified
fork advantages only. Detail catalog:
[Fork-specific features](fork/harness/upstream-development-guide.md#fork-specific-features-for-ai-agents).

| Need | Guide |
|------|--------|
| Merge / overlays | [`fork/harness/AGENTS.md`](fork/harness/AGENTS.md) |
| Hypura `harness_*` / daemon | [`fork/agent-harness/AGENTS.md`](fork/agent-harness/AGENTS.md) |
| Plugins / fork tools | [`fork/extensions/AGENTS.md`](fork/extensions/AGENTS.md) |
| Windows stack / ports | [`fork/operations/AGENTS.md`](fork/operations/AGENTS.md) |
| Root scratch | [`fork/local-workspace/AGENTS.md`](fork/local-workspace/AGENTS.md) |

Secrets in `.env` only; non-secret behaviour in `config.yaml`. No new non-secret
`HERMES_*` env vars. Do not commit `_docs/`, media, release bundles, or
`node_modules/`.

## Root Layout Policy

Match upstream root style: packaging/entry Python (`run_agent.py`, `cli.py`,
`model_tools.py`, …), `scripts/`, `docs/`, `tests/`, apps/UI packages stay at
their official paths — **do not relocate** for “cleanliness” (imports, CI, and
`pyproject` assume them).

| Stay at root (intentional) | Why |
|----------------------------|-----|
| Core `*.py` entry modules, `pyproject.toml`, lockfiles | Official packaging surface |
| `hermes_api_server.py`, `sync_memory.py`, `requirements.txt` | Fork helpers wired by tests/scripts |
| `fork/`, `vendor/`, `brain/`, `SOUL.md` (local) | Fork / identity; not upstream PR material |

| Move / classify | Destination |
|-----------------|-------------|
| One-off probes, `tmp_*.py`, tweet drafts, comparison notes | `tmp/probes/` or `output/reports/` (gitignored) |
| Tracked operator notes (`TASK_SUMMARY`, release tweet drafts) | `fork/local-workspace/notes/` |
| Generated media / logs / `_docs` | `output/*`, `_docs/` — never publish |

See [`fork/local-workspace/README.md`](fork/local-workspace/README.md).

## Development Snapshot

```bash
# Prefer project .venv (Python 3.12 via uv); fall back only if needed
source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
scripts/run_tests.sh        # always — not bare pytest
```

User config: `~/.hermes/config.yaml` + `~/.hermes/.env` (secrets). Paths:
`get_hermes_home()` / `display_hermes_home()` — never hardcode `~/.hermes`.

Desktop: [`apps/desktop/AGENTS.md`](apps/desktop/AGENTS.md). Tools: register in
`tools/` + wire into `toolsets.py`. Plugins must not patch core files.

## Learned User Preferences

- Windows stack restarts include Desktop by default; exclude llama unless
  explicitly requested. With llama: `restart-hermes-stack.ps1 -StartLlama` via
  hot-swap (`start-llama-hotswap.ps1` / `llama-hotswap-models.ini`) or
  Turboquant / RTX 5060 Ti scripts; wait until warm/ready — if `:8080`
  `/v1/models` is already healthy, omit `-StartLlama`. No new non-secret
  `HERMES_*`.
- Prefer Desktop `.lnk` (Hermes icon) over `.ps1` launchers.
- Keep Desktop↔backend mutual monitoring after restarts; prefer **Go watchdog
  alone** (not parallel PowerShell watchdog). Operator watchdog / control-plane
  HTTP must not be controllable from Hermes AI tools/sessions.
- Avoid duplicate Desktop/backend/watchdog processes; use `-SkipTunnels` when
  memory-graph/tunnels hang. If `-StartGoWatchdog` BuildIfMissing/prewarm hangs:
  packaged exe + `hermes serve --skip-build` on `:9119`, then Desktop. Clean
  Desktop recovery: stop Go watchdog → clear wedged `:9118`/`:9119` → timed HTTP
  on serve (not only LISTEN) → single Desktop; do not disturb WebUI `:8787`.
- Run CLI / restarts / Desktop backend / smoke from uv `.venv` on Python 3.12
  (`uv venv --python 3.12` / `uv sync`); not system 3.14 or wrong `py -3`.
- Upstream merges while live checkout stays up: separate branch/worktree; if
  features equivalent, take upstream and reapply fork advantages via
  `scripts/sync_all.py` / `scripts/merge_tools/`. Preserve fork `self_evolution`
  (`ai_scientist_research` / `shinka_run` + API-key bridge) via overlay replay.
- Upstream security/fix/Dependabot PRs: branch from latest `upstream/main`,
  exclude `_docs`/fork noise, King's English, check for duplicate Issue/PR;
  salvage incomplete prior fixes; harden env-leak / path-traversal /
  agent-runaway without gutting capability. Large framework migrations
  (e.g. react-router v8 / `web/`) stay on dedicated branches — do not fold
  into Dependabot batch or upstream-sync merges.
- New agent-facing folders (esp. harness docs): add `AGENTS.md` + README.

## Learned Workspace Facts

- Packaged Desktop: `%LOCALAPPDATA%\hermes\hermes-agent\apps\desktop\release\win-unpacked\Hermes.exe`
  (fallback: repo `apps/desktop/release/win-unpacked/Hermes.exe`). After Desktop
  code changes: `hermes desktop --build-only --force-build` and sync into
  LocalAppData.
- Go watchdog: `scripts/windows/watchdog-go/` via `Start-HermesGoWatchdog.ps1`
  (default `127.0.0.1:9920`, optional Tailscale). May prewarm serve on `:9118`;
  Desktop expects announcement on `:9119` — mismatch/unready →
  `Timed out waiting for Hermes backend port announcement (90000ms)`. Prefer
  healthy `:9119` (or matching `HERMES_DESKTOP_REMOTE_*`). Paths with spaces:
  separate argv for `-hermes-root` (never PowerShell `$args`); `detectRepoRoot`
  needs `pyproject.toml`, must not stop at `\scripts`. Mutual monitoring:
  `%LOCALAPPDATA%\HermesWatchdog\desktop-backend.json` + `HERMES_DESKTOP_REMOTE_*`.
  Mirror / local hot-swap checkout:
  `C:\Users\downl\Documents\New project\HermesDesktopwatchdog` (GitHub:
  zapabob/HermesDesktopwatchdog). After Go watchdog or post-merge Desktop
  announce/auth changes: rebuild from that checkout or
  `Build-HermesGoWatchdog.ps1` (e.g. `-SkipTest`) before `-StartGoWatchdog`.
- Prewarm/managed ports: `/api/status` or LISTEN alone is insufficient (wedged
  serve can hang HTTP at 0 bytes). Manifest token drift → `/api/sessions` 401 /
  “Could not connect”; Go watchdog `testBackendAuth` before publishing and
  replaces on drift. Dead `:9119` may temporarily use healthy dashboard `:9120`
  via `HERMES_DESKTOP_REMOTE_*`; clear stale overrides when recovering. Legacy
  PS watchdog: `Start-HermesDesktopBackendWatchdog.ps1` (prefer Go-only).
- Port map: [`fork/operations/AGENTS.md`](fork/operations/AGENTS.md) — **8787 =
  Hermes-WebUI** (not messaging gateway); 9118 prewarm; 9119 Desktop serve;
  9120 dashboard; llama 8080/8081; harness 18794. Reserved ops ports must not be
  treated as Desktop backend or reaped by the watchdog.
- On this Windows host, `Get-CimInstance` / `netstat` (sometimes `taskkill`) can
  hang during cleanup — prefer `Stop-Process -Name` + short `curl.exe -m`.
- Worktrees that cannot check out `main`: `git push origin HEAD:main`.
- Local llama.cpp: context ≥ 65536 (often ~100k); GPU = RTX 5060 Ti 16GB;
  Turboquant scripts under `scripts/windows/`. Hot-swap:
  `start-llama-hotswap.ps1` + `llama-hotswap-models.ini` with isolated HF-cache
  (recent `:8080` lineup: Qwen3.6-35B IQ3_M + Huihui-gemma-4-12B-agentic
  Q4_K_M). Do not invent HF ids like
  `NousResearch/Hermes-3-Llama-3.1-8B-GGUF:Q4_K_M`; if a Hermes-3 stub reappears
  on `/v1/models`, ForceRestart the isolated hot-swap stack.
- World Intel MCP (`zapabob/world-intel-mcp`, toolset often `world-intel`):
  provider keys in `~/.hermes/.env` only; dashboard
  `.venv\Scripts\intel-dashboard.exe` → `http://localhost:8501` (Tailscale for
  remote).
- Missing Desktop session history / post-merge boot failure: treat as
  Desktop↔`hermes serve` connectivity/auth first (`:9119` LISTEN-but-HTTP-000
  zombie, stale `HERMES_DESKTOP_REMOTE_*`). URL set without TOKEN is a hard fail —
  clear both and use local `:9119`; also check manifest drift / 401.
  `~/.hermes/state.db` is often intact — do not VACUUM/rewrite the live DB
  while other agents may use it.
