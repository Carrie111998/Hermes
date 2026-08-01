"""Component library + default seed dashboards for Amorphous Applications.

A dashboard *spec* is JSON:
{
  "title": "...",
  "user_id": "...",
  "grid": {"columns": 12},
  "components": [
     {"id": "...", "type": "<registry type>", "title": "...",
      "col": 0, "row": 0, "w": 4, "h": 2, "props": {...}, "hidden": false}
  ],
  "chat_dock": {"position": "bottom", "visible": true}
}

Component *types* are the pre-built library the curator composes from.
Each type declares a renderer key the frontend knows, plus a data endpoint
contract the server fulfils.
"""

from __future__ import annotations

import copy
import uuid

COMPONENT_LIBRARY: dict[str, dict] = {
    "metric": {
        "name": "Metric tile",
        "description": "Single headline number with delta (from a datasource query).",
        "min_w": 2, "min_h": 1,
    },
    "timeseries": {
        "name": "Timeseries chart",
        "description": "Line/area chart from a datasource query.",
        "min_w": 4, "min_h": 2,
    },
    "table": {
        "name": "Data table",
        "description": "Tabular records from a datasource query.",
        "min_w": 4, "min_h": 2,
    },
    "workflow_button": {
        "name": "Workflow shortcut",
        "description": "One-click repeatable agent workflow; surfaces last run result.",
        "min_w": 2, "min_h": 1,
    },
    "workflow_panel": {
        "name": "Workflow panel",
        "description": "Parameterized agent workflow with input fields + run history.",
        "min_w": 4, "min_h": 2,
    },
    "agent_activity": {
        "name": "Agent activity feed",
        "description": "Recent Hermes sessions, cron jobs, and workflow runs.",
        "min_w": 3, "min_h": 2,
    },
    "notes": {
        "name": "Notes / briefing",
        "description": "Agent-maintained markdown briefing (curator can rewrite).",
        "min_w": 3, "min_h": 2,
    },
    "datasource_status": {
        "name": "Datasource status",
        "description": "Connection health for external datasources.",
        "min_w": 2, "min_h": 1,
    },
    "quick_links": {
        "name": "Quick links",
        "description": "Curated links (Confluence pages, runbooks, dashboards).",
        "min_w": 2, "min_h": 1,
    },
    "evolution_log": {
        "name": "Evolution log",
        "description": "History of curator proposals and applied mutations.",
        "min_w": 3, "min_h": 2,
    },
}


def new_component(ctype: str, title: str, col: int, row: int, w: int, h: int,
                  props: dict | None = None, cid: str | None = None) -> dict:
    if ctype not in COMPONENT_LIBRARY:
        raise ValueError(f"unknown component type: {ctype}")
    return {
        "id": cid or f"{ctype}-{uuid.uuid4().hex[:6]}",
        "type": ctype,
        "title": title,
        "col": col, "row": row, "w": w, "h": h,
        "props": props or {},
        "hidden": False,
    }


def seed_layout(user_id: str, persona: str = "sre") -> dict:
    """Default mission-control layout for a new user (SRE-flavoured demo persona)."""
    comps = [
        new_component("metric", "API p95 latency", 0, 0, 3, 1,
                      {"datasource": "datadog", "query": "p95.latency", "unit": "ms"},
                      cid="m-latency"),
        new_component("metric", "Error rate", 3, 0, 3, 1,
                      {"datasource": "datadog", "query": "error.rate", "unit": "%"},
                      cid="m-errors"),
        new_component("metric", "Uptime (30d)", 6, 0, 3, 1,
                      {"datasource": "betterstack", "query": "uptime.30d", "unit": "%"},
                      cid="m-uptime"),
        new_component("datasource_status", "Datasources", 9, 0, 3, 1, {},
                      cid="ds-status"),
        new_component("timeseries", "Request volume", 0, 1, 6, 2,
                      {"datasource": "datadog", "query": "requests.volume"},
                      cid="ts-requests"),
        new_component("table", "Open incidents", 6, 1, 6, 2,
                      {"datasource": "betterstack", "query": "incidents.open"},
                      cid="tbl-incidents"),
        new_component("workflow_button", "Triage latest incident", 0, 3, 3, 1,
                      {"workflow_id": "wf-triage"}, cid="wf-btn-triage"),
        new_component("workflow_button", "Daily standup summary", 3, 3, 3, 1,
                      {"workflow_id": "wf-standup"}, cid="wf-btn-standup"),
        new_component("workflow_panel", "Ask about a service", 6, 3, 6, 2,
                      {"workflow_id": "wf-service-report",
                       "inputs": [{"name": "service", "label": "Service name"}]},
                      cid="wf-panel-service"),
        new_component("table", "Metabase: signups by day", 0, 4, 6, 2,
                      {"datasource": "metabase", "query": "signups.by_day"},
                      cid="tbl-signups"),
        new_component("quick_links", "Runbooks (Confluence)", 0, 6, 3, 1,
                      {"datasource": "confluence", "query": "runbooks"},
                      cid="ql-runbooks"),
        new_component("agent_activity", "Hermes activity", 3, 6, 5, 2, {},
                      cid="agent-activity"),
        new_component("evolution_log", "Dashboard evolution", 8, 6, 4, 2, {},
                      cid="evo-log"),
        new_component("notes", "Morning briefing", 9, 1, 3, 1,
                      {"markdown": "_The curator will maintain this briefing "
                                   "based on what you focus on._"},
                      cid="notes-briefing"),
    ]
    return {
        "title": f"Hermes Station — {user_id}",
        "user_id": user_id,
        "persona": persona,
        "grid": {"columns": 12},
        "components": comps,
        "chat_dock": {"position": "bottom", "visible": True},
    }


SEED_WORKFLOWS = [
    {"id": "wf-triage", "name": "Triage latest incident",
     "description": "Pull the newest open incident and produce a triage plan.",
     "prompt_template": "Review the latest open incident from our monitoring "
                        "(context: {context}). Produce a 5-line triage plan: likely "
                        "cause, blast radius, first mitigation step, who to page, "
                        "and a customer-comms one-liner."},
    {"id": "wf-standup", "name": "Daily standup summary",
     "description": "Summarize the last 24h of dashboard + agent activity.",
     "prompt_template": "Summarize the last 24 hours for a standup update. "
                        "Activity data: {context}. Output 3 bullets: what happened, "
                        "what's at risk, what needs a decision."},
    {"id": "wf-service-report", "name": "Service report",
     "description": "Deep-dive report on a named service.",
     "prompt_template": "Produce a short health report for service '{service}'. "
                        "Metrics context: {context}. Cover latency, errors, recent "
                        "deploys, and one recommendation."},
]


def apply_mutations(spec: dict, mutations: list[dict]) -> dict:
    """Apply curator/user mutations to a layout spec. Returns a new spec.

    Mutation ops:
      promote   {component_id}            -> move toward top-left, grow +1w (cap 6)
      shrink    {component_id}            -> shrink to min size
      hide      {component_id}
      show      {component_id}
      remove    {component_id}
      retitle   {component_id, title}
      add       {component}               -> full component dict
      set_props {component_id, props}     -> shallow-merge props
      set_notes {component_id, markdown}
      move_chat_dock {position}
    """
    spec = copy.deepcopy({k: v for k, v in spec.items() if k != "_meta"})
    comps = spec.setdefault("components", [])
    by_id = {c["id"]: c for c in comps}

    for m in mutations:
        op = m.get("op")
        cid = m.get("component_id")
        c = by_id.get(cid) if cid else None
        if op == "promote" and c:
            comps.remove(c)
            comps.insert(0, c)
            c["row"] = 0
            c["w"] = min(int(c.get("w", 3)) + 1, 6)
        elif op == "shrink" and c:
            lib = COMPONENT_LIBRARY.get(c["type"], {})
            c["w"] = lib.get("min_w", 2)
            c["h"] = lib.get("min_h", 1)
        elif op == "hide" and c:
            c["hidden"] = True
        elif op == "show" and c:
            c["hidden"] = False
        elif op == "remove" and c:
            comps.remove(c)
            by_id.pop(cid, None)
        elif op == "retitle" and c:
            c["title"] = m.get("title", c["title"])
        elif op == "add" and m.get("component"):
            nc = m["component"]
            if nc.get("type") in COMPONENT_LIBRARY and nc.get("id") not in by_id:
                nc.setdefault("hidden", False)
                nc.setdefault("props", {})
                comps.append(nc)
                by_id[nc["id"]] = nc
        elif op == "set_props" and c:
            c.setdefault("props", {}).update(m.get("props", {}))
        elif op == "set_notes" and c and c["type"] == "notes":
            c.setdefault("props", {})["markdown"] = m.get("markdown", "")
        elif op == "move_chat_dock":
            spec.setdefault("chat_dock", {})["position"] = m.get("position", "bottom")

    _reflow(spec)
    return spec


def _reflow(spec: dict) -> None:
    """Greedy re-pack of visible components into the grid, preserving order."""
    columns = spec.get("grid", {}).get("columns", 12)
    col = row = 0
    row_h = 1
    for c in spec.get("components", []):
        if c.get("hidden"):
            continue
        w = min(int(c.get("w", 3)), columns)
        h = int(c.get("h", 1))
        if col + w > columns:
            col = 0
            row += row_h
            row_h = 1
        c["col"], c["row"], c["w"] = col, row, w
        col += w
        row_h = max(row_h, h)
