"""Component library, templates, and layout mutation engine for Hermes Station.

Grid model: 12 columns × fixed row height (110px). Components declare w (cols)
and h (rows). The reflow packer places visible components greedily with NO gaps:
it always drops each card into the leftmost-topmost slot it fits (skyline pack),
which eliminates stray whitespace even with mixed sizes.

Data components carry props {source, query:{...}} resolved by datasources.py.
"""

from __future__ import annotations

import copy
import uuid

COMPONENT_LIBRARY: dict[str, dict] = {
    "metric":          {"name": "Metric tile", "min_w": 2, "min_h": 1},
    "timeseries":      {"name": "Chart", "min_w": 3, "min_h": 2},
    "table":           {"name": "Table", "min_w": 4, "min_h": 2},
    "kv":              {"name": "Key/value panel", "min_w": 2, "min_h": 1},
    "feed":            {"name": "Activity feed", "min_w": 3, "min_h": 2},
    "links":           {"name": "Link list", "min_w": 2, "min_h": 1},
    "workflow_button": {"name": "Workflow shortcut", "min_w": 2, "min_h": 1},
    "workflow_panel":  {"name": "Workflow panel", "min_w": 3, "min_h": 2},
    "notes":           {"name": "Notes / briefing", "min_w": 2, "min_h": 1},
    "connections":     {"name": "Connections status", "min_w": 2, "min_h": 1},
}


def new_component(ctype: str, title: str, w: int, h: int,
                  props: dict | None = None, cid: str | None = None) -> dict:
    if ctype not in COMPONENT_LIBRARY:
        raise ValueError(f"unknown component type: {ctype}")
    return {"id": cid or f"{ctype}-{uuid.uuid4().hex[:6]}", "type": ctype,
            "title": title, "w": w, "h": h, "props": props or {},
            "hidden": False}


# ---------------------------------------------------------------- templates

def _dev_template(user_id: str, repo: str = ".") -> list[dict]:
    return [
        new_component("kv", "Repo status", 3, 2,
                      {"source": "git.status", "query": {"repo": repo}}, "dev-status"),
        new_component("kv", "System", 3, 2,
                      {"source": "system.stats", "query": {}}, "dev-system"),
        new_component("workflow_button", "Review my uncommitted diff", 3, 2,
                      {"workflow_id": "wf-review-diff"}, "dev-wf-review"),
        new_component("workflow_button", "Summarize repo activity", 3, 2,
                      {"workflow_id": "wf-repo-summary"}, "dev-wf-summary"),
        new_component("table", "Commit history", 6, 3,
                      {"source": "git.log", "query": {"repo": repo, "limit": 10}}, "dev-log"),
        new_component("table", "Open PRs", 6, 3,
                      {"source": "github.prs", "query": {"cwd": repo, "limit": 8}}, "dev-prs"),
        new_component("table", "Open issues", 6, 3,
                      {"source": "github.issues", "query": {"cwd": repo, "limit": 8}}, "dev-issues"),
        new_component("links", "Hacker News", 3, 3,
                      {"source": "rss", "query": {"url": "https://hnrss.org/frontpage", "limit": 7}}, "dev-hn"),
        new_component("feed", "Station activity", 3, 3,
                      {"source": "station.activity", "query": {}}, "dev-activity"),
        new_component("notes", "Briefing", 3, 2,
                      {"markdown": "Hermes maintains this from your usage."}, "dev-notes"),
    ]


def _trader_template(user_id: str) -> list[dict]:
    return [
        new_component("table", "Prices", 4, 3,
                      {"source": "crypto.price",
                       "query": {"coins": "bitcoin,ethereum,solana,dogecoin"}}, "tr-prices"),
        new_component("timeseries", "BTC 7d", 4, 2,
                      {"source": "crypto.chart", "query": {"coin": "bitcoin"}}, "tr-btc"),
        new_component("timeseries", "ETH 7d", 4, 2,
                      {"source": "crypto.chart", "query": {"coin": "ethereum"}}, "tr-eth"),
        new_component("workflow_button", "Morning market brief", 3, 2,
                      {"workflow_id": "wf-market-brief"}, "tr-wf-brief"),
        new_component("workflow_panel", "Analyze an asset", 5, 2,
                      {"workflow_id": "wf-asset-analysis",
                       "inputs": [{"name": "asset", "label": "Asset (e.g. solana)"}]}, "tr-wf-asset"),
        new_component("links", "Market news", 4, 2,
                      {"source": "rss",
                       "query": {"url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "limit": 7}}, "tr-news"),
        new_component("feed", "Station activity", 3, 2,
                      {"source": "station.activity", "query": {}}, "tr-activity"),
        new_component("notes", "Trade notes", 3, 2,
                      {"markdown": "Ideas and levels — Hermes keeps this current."}, "tr-notes"),
    ]


def _exec_template(user_id: str) -> list[dict]:
    return [
        new_component("workflow_button", "Daily exec brief", 3, 2,
                      {"workflow_id": "wf-exec-brief"}, "ex-wf-brief"),
        new_component("workflow_panel", "Research a competitor", 5, 2,
                      {"workflow_id": "wf-competitor",
                       "inputs": [{"name": "company", "label": "Company"}]}, "ex-wf-comp"),
        new_component("links", "Industry news", 4, 2,
                      {"source": "rss", "query": {"url": "https://hnrss.org/frontpage", "limit": 7}}, "ex-news"),
        new_component("kv", "Weather", 3, 2,
                      {"source": "weather", "query": {"lat": 30.27, "lon": -97.74}}, "ex-wx"),
        new_component("table", "Markets", 4, 2,
                      {"source": "crypto.price", "query": {"coins": "bitcoin,ethereum"}}, "ex-mkt"),
        new_component("feed", "Station activity", 3, 2,
                      {"source": "station.activity", "query": {}}, "ex-activity"),
        new_component("notes", "Priorities", 3, 2,
                      {"markdown": "Top 3 priorities — tell Hermes and it will track them."}, "ex-notes"),
    ]


TEMPLATES = {
    "developer": {"name": "Developer", "blurb": "Repos, commits, PRs/issues, code workflows",
                  "builder": _dev_template},
    "trader": {"name": "Trader", "blurb": "Live prices, charts, market briefs",
               "builder": _trader_template},
    "executive": {"name": "Executive", "blurb": "Briefs, research workflows, news, markets",
                  "builder": _exec_template},
    "blank": {"name": "Blank canvas", "blurb": "Start empty; build it in chat with Hermes",
              "builder": lambda user_id, **kw: [
                  new_component("notes", "Getting started", 4, 1,
                                {"markdown": "Ask the chat below to build your dashboard — "
                                             "e.g. \"add my repo's commit log and open PRs\"."},
                                "blank-notes"),
                  new_component("connections", "Connections", 3, 1, {}, "blank-conn"),
              ]},
}

TEMPLATE_WORKFLOWS = {
    "developer": [
        {"id": "wf-review-diff", "name": "Review my uncommitted diff",
         "description": "Reads git diff in the configured repo and reviews it.",
         "prompt_template": "Run `git diff` (and `git diff --staged`) in {repo} via the "
                            "terminal, then review the changes: bugs, risks, missing tests. "
                            "If clean, say so. Context: {context}"},
        {"id": "wf-repo-summary", "name": "Summarize repo activity",
         "description": "Last 24h of commits/PRs/issues.",
         "prompt_template": "Using git log and gh CLI in {repo}, summarize the last 24h: "
                            "commits, opened/merged PRs, new issues. 5 bullets max. Context: {context}"},
    ],
    "trader": [
        {"id": "wf-market-brief", "name": "Morning market brief",
         "description": "Prices + overnight moves + notable news.",
         "prompt_template": "Produce a morning market brief: check current BTC/ETH/SOL prices "
                            "and 24h moves (station_query_datasource crypto.price), scan market "
                            "news, and give 5 bullets: moves, catalysts, one risk. Context: {context}"},
        {"id": "wf-asset-analysis", "name": "Analyze an asset",
         "description": "Price action + news scan for one asset.",
         "prompt_template": "Analyze {asset}: fetch its 7d chart data and current price via "
                            "station_query_datasource, search the web for recent news, and give "
                            "a terse read: trend, key levels, catalysts, risks. Context: {context}"},
    ],
    "executive": [
        {"id": "wf-exec-brief", "name": "Daily exec brief",
         "description": "News + markets + dashboard activity in 5 bullets.",
         "prompt_template": "Produce today's exec brief: scan the news feeds on my dashboard, "
                            "markets, and recent Station activity. 5 bullets: what matters, why, "
                            "any action needed. Context: {context}"},
        {"id": "wf-competitor", "name": "Research a competitor",
         "description": "Web research on a named company.",
         "prompt_template": "Research {company}: recent launches, funding, positioning vs us. "
                            "Web-search as needed. Output: 6 bullets + one strategic implication. "
                            "Context: {context}"},
    ],
    "blank": [],
}


def seed_layout(user_id: str, template: str = "developer", **kw) -> dict:
    t = TEMPLATES.get(template, TEMPLATES["developer"])
    comps = t["builder"](user_id, **kw)
    spec = {
        "title": f"Hermes Station — {user_id}",
        "user_id": user_id,
        "template": template,
        "grid": {"columns": 12, "row_px": 110},
        "components": comps,
        "chat_dock": {"position": "bottom", "visible": True},
    }
    _reflow(spec)
    return spec


# ---------------------------------------------------------------- mutations

def apply_mutations(spec: dict, mutations: list[dict]) -> dict:
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
            c["w"] = min(int(c.get("w", 3)) + 1, 8)
        elif op == "shrink" and c:
            lib = COMPONENT_LIBRARY.get(c["type"], {})
            c["w"], c["h"] = lib.get("min_w", 2), lib.get("min_h", 1)
        elif op == "resize" and c:
            c["w"] = max(1, min(int(m.get("w", c["w"])), 12))
            c["h"] = max(1, min(int(m.get("h", c["h"])), 6))
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
            if nc.get("type") in COMPONENT_LIBRARY:
                nc.setdefault("id", f"{nc['type']}-{uuid.uuid4().hex[:6]}")
                if nc["id"] not in by_id:
                    nc.setdefault("hidden", False)
                    nc.setdefault("props", {})
                    nc.setdefault("w", COMPONENT_LIBRARY[nc["type"]]["min_w"])
                    nc.setdefault("h", COMPONENT_LIBRARY[nc["type"]]["min_h"])
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
    """Position-preserving placement: components that already carry col/row are
    LEFT ALONE (the user may have dragged them there). Only components missing
    coordinates (fresh adds) are skyline-packed into free space. The frontend
    grid (react-grid-layout) resolves any residual overlap by vertical
    compaction, so we never fight user-chosen positions."""
    columns = spec.get("grid", {}).get("columns", 12)
    occupied: set[tuple[int, int]] = set()

    def mark(col: int, row: int, w: int, h: int) -> None:
        for cc in range(col, col + w):
            for rr in range(row, row + h):
                occupied.add((cc, rr))

    def fits(col: int, row: int, w: int, h: int) -> bool:
        if col + w > columns:
            return False
        return all((c, r) not in occupied
                   for c in range(col, col + w) for r in range(row, row + h))

    pending = []
    for comp in spec.get("components", []):
        if comp.get("hidden"):
            continue
        w = max(1, min(int(comp.get("w", 3)), columns))
        h = max(1, int(comp.get("h", 1)))
        if comp.get("col") is not None and comp.get("row") is not None:
            mark(int(comp["col"]), int(comp["row"]), w, h)
        else:
            pending.append((comp, w, h))

    for comp, w, h in pending:
        row = 0
        while True:
            placed = False
            for col in range(0, columns - w + 1):
                if fits(col, row, w, h):
                    comp["col"], comp["row"] = col, row
                    mark(col, row, w, h)
                    placed = True
                    break
            if placed:
                break
            row += 1
