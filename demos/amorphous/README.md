# Amorphous Applications — PoC

A Hermes-powered, self-evolving "mission control" dashboard. Every user/org gets a
personal dashboard composed from a pre-built component library. The dashboard:

- surfaces Hermes agent activity, workflow shortcuts, and external datasources
  (Datadog / BetterStack / Metabase / Confluence — demo connectors included),
- has an **invariant agent chat dock** (bottom-center, hideable, dockable right),
- collects rich interaction telemetry (clicks, hover/focus dwell time, workflow
  runs, chat topics, hide/move/resize actions),
- runs an **evolution curator** on an interval (demo: on-demand or every N minutes;
  production: hourly/6h/daily) that reviews the period's telemetry and proposes
  dashboard mutations — promote hot components, shrink/retire cold ones, mint new
  workflow shortcuts from repeated chat prompts,
- routes every mutation through an **approval + feedback** tray,
- supports full **chat-prompted rebuild** (`/rebuild ...` in the chat dock).

## Run

```bash
python demos/amorphous/serve.py            # http://127.0.0.1:8877
python demos/amorphous/serve.py --port 9000 --curator-minutes 60
python demos/amorphous/simulate.py         # generate a period of synthetic usage
```

No API key needed: with `NOUS_API_KEY`/`OPENROUTER_API_KEY`/`OPENAI_API_KEY` (or
`~/.hermes/.env`) the chat dock and curator use a real LLM; otherwise everything
falls back to a deterministic heuristic engine so the evolution loop still demos.

## Layout of the PoC

| File | Purpose |
|---|---|
| `store.py` | SQLite: layouts (versioned), telemetry events, proposals, workflows, feedback |
| `components.py` | Component registry + default seed dashboards |
| `datasources.py` | Datasource connectors (generic HTTP + Datadog/BetterStack/Metabase/Confluence demo adapters) |
| `agent_bridge.py` | LLM/agent access: Hermes env keys → OpenAI-compatible chat; offline fallback |
| `curator.py` | Evolution engine: telemetry review → mutation proposals (heuristics + optional LLM) |
| `server.py` | FastAPI app: REST + WebSocket chat + curator scheduler |
| `static/` | Frontend (vanilla JS/CSS grid, chat dock, proposal tray) |
| `serve.py` | Entry point |
| `simulate.py` | Synthetic usage generator to demo evolution |
