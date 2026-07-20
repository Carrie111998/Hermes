# Running Hermes from this fork (dev loop)

Goal: run the **Hermes Desktop app** against **this checkout** so that edits to
both the **UI** (React/Electron) and the **Python backend** show up in what we're
running — no reinstall, no copy.

## Why this works

The desktop app is only an Electron shell. The real agent runs as a Python
`hermes serve` process that Electron spawns. In **dev mode** (not a packaged
installer), Electron's resolver picks this repo automatically:

- It computes `SOURCE_REPO_ROOT` as the repo root and, when not packaged, uses it
  ahead of any installed `hermes` on PATH — see
  [`apps/desktop/electron/main.ts`](apps/desktop/electron/main.ts) (`resolveHermesBackend`, ~L3442).
- The backend is launched with **this checkout on `PYTHONPATH`**, so `agent/`,
  `tools/`, `cli.py`, `hermes_cli/`, etc. are imported straight from the working
  tree. The venv only supplies third-party dependencies.

Result:
- **UI edits** → live via Vite HMR.
- **Python edits** → live from the tree; take effect on the next app/backend restart.

## One-time setup

The resolver looks for a venv at the repo root named `.venv` (preferred) or
`venv`. `uv` creates `.venv`, so this lines up:

```bash
# from repo root
npm install                      # links workspaces (apps/desktop, apps/shared, web)
uv sync --extra all --extra dev  # creates .venv at root; installs deps + editable project
```

Pip alternative (Windows paths shown):

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[all,dev]"
```

## Run it (repeat every time)

```bash
cd apps/desktop
npm run dev
```

**This one command runs everything — you do NOT start the backend separately.**
`npm run dev` launches the Vite UI server, then Electron, and **Electron itself
spawns the Python backend** (`python -m hermes_cli.main serve` from `.venv`, with
this checkout on `PYTHONPATH`). No second terminal, no separate `hermes serve`.

- Edit React → hot reloads.
- Edit Python → restart the app (or backend) to pick it up.

(Running `hermes` / `hermes serve` yourself is only for using the CLI/TUI or web
dashboard standalone — it is not part of the desktop dev loop.)

## Useful env overrides

- `HERMES_DESKTOP_HERMES_ROOT=<path-to-this-repo>` — force any build (even a
  *packaged* app) to use this checkout. Always wins (step 1 of the resolver).
- `HERMES_DESKTOP_PYTHON=<python>` — force a specific interpreter.
- `HERMES_HOME=<dir>` — point config/sessions at a throwaway dir to sandbox from
  the real install. Default on Windows is `%LOCALAPPDATA%\hermes`.

## Notes

- **Don't use a packaged installer for the dev loop.** `npm run dist:win` bakes
  the UI into an asar bundle — no live editing. Installers are for shipping.
- The editable install also means the plain `hermes` CLI/TUI runs from this same
  tree, so backend changes show up there too.
- Boot/backend logs: `HERMES_HOME/logs/desktop.log`.
