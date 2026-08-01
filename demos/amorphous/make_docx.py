#!/usr/bin/env python3
"""Generate the Amorphous Applications overview docx."""
from docx import Document
from docx.shared import Pt, RGBColor
from pathlib import Path

doc = Document()

def h(text, level=1):
    doc.add_heading(text, level=level)

def p(text, bold=False):
    para = doc.add_paragraph()
    run = para.add_run(text)
    run.bold = bold
    return para

def bullets(items):
    for it in items:
        doc.add_paragraph(it, style="List Bullet")

title = doc.add_heading("Amorphous Applications", 0)
p("A Hermes-powered, self-evolving mission-control dashboard — Proof of Concept", bold=True)
p("Nous Research · August 2026 · demos/amorphous in the hermes-agent repo")

h("1. The Idea")
p("Every person, team, or company gets a personal mission-control surface that is "
  "HERMES-powered and HERMES-shaped: the agent builds it, watches how it is used, and "
  "continuously reshapes it around the user's actual needs. It surfaces agent activity, "
  "one-click repeatable workflows, and internal + external datasources (public and private), "
  "with the goal of becoming the only interaction layer the user needs to do their job. "
  "The dashboard is 'amorphous' because it has no fixed final form — it evolves with usage.")

h("2. What the PoC Demonstrates")
bullets([
    "A local web server (FastAPI) serving a per-user dashboard composed from a pre-built component library.",
    "An invariant agent chat dock — bottom-center by default, movable to a right side panel, collapsible, but always present.",
    "Workflow components: buttons and parameterized panels that trigger repeatable Hermes workflows and surface results inline.",
    "External datasource connectors: Datadog, Better Stack, Metabase, Confluence (live when credentials exist, deterministic demo data otherwise) plus the internal Hermes activity source.",
    "Full-fidelity interaction telemetry: views, clicks, hover-dwell seconds, hide/move/resize actions, workflow runs, chat prompts, proposal decisions.",
    "An evolution curator that reviews each period's telemetry and proposes concrete dashboard mutations — never auto-applied.",
    "An approval + feedback system: proposals land in a tray with per-mutation explanations; the user approves/rejects with optional feedback text that future curator runs read.",
    "Chat-prompted rebuild: '/rebuild <describe what you want>' makes the agent redesign the entire dashboard as a reviewable proposal.",
    "Versioned layout history: every applied change creates a new layout version tagged with its source (seed / user / curator / rebuild), so evolution is auditable and reversible by design.",
])

h("3. Architecture & Primitives")
p("The PoC decomposes into seven primitives, each an isolated module:")

t = doc.add_table(rows=1, cols=2)
t.style = "Light Grid Accent 1"
hdr = t.rows[0].cells
hdr[0].text = "Primitive"
hdr[1].text = "Responsibility"
rows = [
    ("Layout store (store.py)", "Versioned dashboard specs per user; append-only history with source attribution."),
    ("Telemetry store (store.py)", "Raw interaction events + per-component aggregation (clicks, dwell seconds, workflow runs, hides/moves)."),
    ("Component library (components.py)", "10 pre-built component types: metric, timeseries, table, workflow_button, workflow_panel, agent_activity, notes, datasource_status, quick_links, evolution_log. Plus the mutation engine (promote/shrink/hide/remove/add/retitle/set_props/replace_spec) and a grid reflow packer."),
    ("Datasource connectors (datasources.py)", "Uniform query(source, name) -> typed payload contract; live API when credentials exist, drifting demo data otherwise."),
    ("Agent bridge (agent_bridge.py)", "Chat + structured JSON tasks against any OpenAI-compatible endpoint (Nous → OpenRouter → OpenAI key resolution, reads ~/.hermes/.env); deterministic offline fallback so the demo runs with zero credentials."),
    ("Evolution curator (curator.py)", "Heuristic engine (always) + LLM refinement (when live): scores component usage, promotes hot panels, shrinks/hides cold ones, mints workflow shortcuts from repeated chat prompts, maintains the briefing note, and honors past user feedback."),
    ("Server + UI (server.py, static/)", "REST API, scheduled curator loop, and the frontend: CSS-grid dashboard, chat dock, proposal tray, telemetry collector."),
]
for name, desc in rows:
    r = t.add_row().cells
    r[0].text = name
    r[1].text = desc

h("4. The Evolution Loop")
p("1. Collect — every interaction is batched to /api/telemetry (4s cadence): what the user "
  "clicks, how long they hover-focus each panel, which workflows they run, what they ask in chat.")
p("2. Review — on a schedule (configurable: hourly / 6h / daily; demo default 6h plus an "
  "'Evolve now' button and a /evolve chat command) the curator aggregates the period and drafts mutations:")
bullets([
    "Hot components (weighted score of clicks ×2, dwell ÷15s, workflow runs ×3) → promoted to the top and enlarged.",
    "Cold components (zero interactions) → shrunk to minimum size, then hidden the following period (always restorable).",
    "Components the user manually hid → proposed for permanent removal.",
    "Chat prompts repeated 3+ times → a new saved workflow is minted and a one-click shortcut component is added.",
    "The 'Morning briefing' notes panel is rewritten with a usage recap.",
])
p("3. Refine — when an LLM is available, it receives the stats, the current layout, recent "
  "user feedback, and the heuristic draft, and returns an improved mutation set with better "
  "titles and a human rationale. In the live demo run, the LLM renamed the auto-minted "
  "shortcut from truncated question text to 'Check api-gateway deploy status'.")
p("4. Approve — the proposal appears in the tray with itemized changes, the engine that "
  "produced it, and the rationale. The user approves or rejects, optionally with feedback "
  "('too aggressive', 'never hide the signup table') that the next curator run sees.")
p("5. Apply — approval writes a new layout version tagged 'curator'; the evolution_log "
  "component shows the full history.")

h("5. Verified Demo Runs (E2E)")
bullets([
    "Offline E2E: 14 components render data; workflows execute; simulated SRE usage (168 events) produced a 12-mutation proposal (2 promotes, 6 shrinks, 1 remove, 2 minted workflows, briefing refresh); approval created layout v3 with the triage workflow and incident table promoted to the top row.",
    "Live-LLM E2E (Claude via OpenRouter): curator refined the draft to 7 mutations with semantic titles and a written rationale; '/rebuild growth-focused view' produced a coherent 'Growth Dashboard' honoring all three user constraints; '/rebuild back to a balanced ops view' restored an ops layout with repaired workflow bindings.",
])

h("6. Roadmap: PoC → Product")
bullets([
    "Replace the HTTP agent bridge with a first-class AIAgent session (full toolset: terminal, browser, delegation) so workflow components can run real multi-step agent tasks with streaming progress.",
    "Real datasource OAuth + query builders; agent-authored connector configs ('connect our Metabase and add churn by cohort').",
    "Component sandbox: let Hermes generate bespoke components (custom HTML/JS in an iframe contract) when the library lacks a fit — the truly 'amorphous' tier.",
    "Multi-user/org: per-team shared dashboards with role-scoped sections; Nous Portal hosting.",
    "Richer telemetry: scroll depth, viewport visibility time, workflow result engagement, A/B of curator proposals.",
    "Guardrails: mutation budgets per period, protected components, one-click rollback to any layout version (data already stored).",
    "Cron-scheduled curator via the existing Hermes cron subsystem instead of the in-process timer.",
])

h("7. Running It")
p("python demos/amorphous/serve.py            # http://127.0.0.1:8877")
p("python demos/amorphous/simulate.py         # synthesize a period of usage")
p("Then press '⚗ Evolve now', review the proposal tray, approve, and watch the dashboard "
  "reshape. Type '/rebuild <anything>' in the chat dock for a full agent redesign.")

out = Path(__file__).parent / "Amorphous-Applications-PoC.docx"
doc.save(out)
print(f"wrote {out}")
