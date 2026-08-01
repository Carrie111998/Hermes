"""Evolution curator for Amorphous Applications.

Reviews a period of interaction telemetry and proposes dashboard mutations.
Two engines, layered:

  1. Heuristic engine (always runs, deterministic, demo-safe):
       - hot components (clicks + dwell + workflow runs)   -> promote
       - cold visible components (no interaction all period)-> shrink, then hide
       - components the user manually hid                   -> propose remove
       - repeated similar chat prompts                      -> mint a workflow
         shortcut component so it becomes one click
       - refresh the 'notes' briefing with a usage recap
  2. LLM engine (when credentials exist): gets the stats + heuristic draft and
     may refine/extend the mutation list with better titles/rationale.

Output is a *proposal* (never auto-applied): the user approves/rejects in the
dashboard tray, optionally with feedback text that future runs see.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter

from agent_bridge import BRIDGE
from components import COMPONENT_LIBRARY, new_component
from store import Store

HOT_SCORE = 8.0
COLD_GRACE_TYPES = {"evolution_log", "datasource_status"}  # never auto-hidden
REPEAT_PROMPT_THRESHOLD = 3


def _score(stats: dict) -> float:
    return (stats.get("clicks", 0) * 2.0
            + stats.get("dwell_s", 0.0) / 15.0
            + stats.get("workflow_runs", 0) * 3.0
            + stats.get("views", 0) * 0.1)


def _normalize_prompt(p: str) -> str:
    p = re.sub(r"[^a-z0-9 ]", "", p.lower())
    words = [w for w in p.split() if len(w) > 3][:6]
    return " ".join(sorted(words))


def run_curator(store: Store, user_id: str, since_ts: float) -> dict | None:
    """One curator pass. Returns the created proposal (or None if nothing to do)."""
    spec = store.get_active_layout(user_id)
    if not spec:
        return None
    usage = store.usage_stats(user_id, since_ts)
    comp_stats = usage["components"]
    if usage["event_count"] == 0:
        return None

    comps = {c["id"]: c for c in spec.get("components", [])}
    mutations: list[dict] = []
    notes: list[str] = []

    # supersede any stale pending proposals — one live proposal at a time
    for p in store.list_proposals(user_id, status="pending"):
        store.resolve_proposal(p["id"], "superseded")

    # ---- hot components: promote ----
    scored = sorted(((cid, _score(s)) for cid, s in comp_stats.items()),
                    key=lambda t: -t[1])
    promoted = 0
    for cid, score in scored:
        c = comps.get(cid)
        if not c or c.get("hidden") or score < HOT_SCORE or promoted >= 2:
            continue
        if c.get("row", 9) == 0 and promoted == 0 and cid == scored[0][0]:
            continue  # already on top
        mutations.append({"op": "promote", "component_id": cid})
        notes.append(f"'{c['title']}' is your most-used panel (score {score:.0f}) — moved up and enlarged.")
        promoted += 1

    # ---- cold components: shrink then hide ----
    for cid, c in comps.items():
        if c.get("hidden") or c["type"] in COLD_GRACE_TYPES:
            continue
        s = comp_stats.get(cid)
        if s is None or _score(s) == 0:
            lib = COMPONENT_LIBRARY.get(c["type"], {})
            already_min = (c.get("w") == lib.get("min_w") and c.get("h") == lib.get("min_h"))
            if already_min:
                mutations.append({"op": "hide", "component_id": cid})
                notes.append(f"'{c['title']}' went untouched this period — hidden (restorable).")
            else:
                mutations.append({"op": "shrink", "component_id": cid})
                notes.append(f"'{c['title']}' saw no use — shrunk to reclaim space.")

    # ---- user-hidden components: propose removal ----
    for cid, s in comp_stats.items():
        c = comps.get(cid)
        if c and s.get("hidden", 0) > 0 and c.get("hidden"):
            mutations.append({"op": "remove", "component_id": cid})
            notes.append(f"You hid '{c['title']}' yourself — proposing permanent removal.")

    # ---- repeated chat prompts -> workflow shortcut ----
    buckets: Counter[str] = Counter()
    exemplar: dict[str, str] = {}
    for p in usage["chat_prompts"]:
        if p.startswith("/"):
            continue
        key = _normalize_prompt(p)
        if key:
            buckets[key] += 1
            exemplar.setdefault(key, p)
    existing_wf_prompts = {w["prompt_template"] for w in store.list_workflows(user_id)}
    for key, count in buckets.most_common(2):
        if count < REPEAT_PROMPT_THRESHOLD:
            break
        prompt = exemplar[key]
        template = prompt + "\n\n(Context: {context})"
        if template in existing_wf_prompts:
            continue
        title = prompt[:42] + ("…" if len(prompt) > 42 else "")
        wf = store.create_workflow(user_id, name=title, prompt_template=template,
                                   description=f"Auto-created: you asked this {count}× this period.",
                                   created_by="curator")
        comp = new_component("workflow_button", title, 0, 0, 3, 1,
                             {"workflow_id": wf["id"]})
        mutations.append({"op": "add", "component": comp})
        notes.append(f"You asked \"{title}\" {count}× — minted a one-click workflow for it.")

    # ---- refresh briefing note ----
    briefing = _briefing(usage, notes)
    for cid, c in comps.items():
        if c["type"] == "notes":
            mutations.append({"op": "set_notes", "component_id": cid,
                              "markdown": briefing})
            break

    if not mutations:
        return None

    rationale = " ".join(notes) or "Periodic layout optimization."
    engine = "heuristic"

    # ---- optional LLM refinement ----
    if BRIDGE.live:
        refined = _llm_refine(spec, usage, mutations, store, user_id)
        if refined:
            mutations, rationale = refined
            engine = "llm"

    summary = f"{len(mutations)} change(s): " + "; ".join(notes[:3])
    return store.create_proposal(user_id, summary=summary, mutations=mutations,
                                 rationale=rationale, engine=engine)


def _briefing(usage: dict, notes: list[str]) -> str:
    lines = [f"**Usage recap** ({time.strftime('%H:%M')}): "
             f"{usage['event_count']} interactions this period."]
    top = sorted(usage["components"].items(), key=lambda t: -_score(t[1]))[:3]
    if top:
        lines.append("Top focus: " + ", ".join(cid for cid, _ in top) + ".")
    lines += [f"- {n}" for n in notes[:4]]
    return "\n".join(lines)


_LLM_SYSTEM = """You are the evolution curator for a user's mission-control dashboard.
You receive usage stats, the current layout, recent user feedback, and a draft mutation list.
Refine the mutations: keep only justified ones, improve titles, and you may add
mutations using ops: promote, shrink, hide, show, remove, retitle, set_props, set_notes,
add (component types: metric,timeseries,table,workflow_button,workflow_panel,
agent_activity,notes,datasource_status,quick_links,evolution_log).
Respect user feedback — if they rejected similar changes before, do not repeat them.
Respond ONLY with JSON: {"mutations": [...], "rationale": "..."}"""


def _llm_refine(spec: dict, usage: dict, draft: list[dict], store: Store,
                user_id: str):
    fb = store.recent_feedback(user_id, limit=10)
    prompt = json.dumps({
        "layout": {c["id"]: {"type": c["type"], "title": c["title"],
                              "hidden": c.get("hidden", False)}
                   for c in spec.get("components", [])},
        "usage": usage["components"],
        "recent_chat": usage["chat_prompts"][-10:],
        "user_feedback": [{"sentiment": f["sentiment"], "text": f["text"]} for f in fb],
        "draft_mutations": draft,
    }, indent=1)
    out = BRIDGE.json_task(prompt, system=_LLM_SYSTEM)
    if isinstance(out, dict) and isinstance(out.get("mutations"), list) and out["mutations"]:
        return out["mutations"], str(out.get("rationale", ""))
    return None


# ---------- chat-prompted rebuild ----------

_REBUILD_SYSTEM = """You design mission-control dashboards from a component library.
Component types: metric, timeseries, table, workflow_button, workflow_panel,
agent_activity, notes, datasource_status, quick_links, evolution_log.
Datasources and queries available:
  datadog: p95.latency (metric,ms), error.rate (metric,%), requests.volume (timeseries)
  betterstack: uptime.30d (metric,%), incidents.open (table)
  metabase: signups.by_day (table)
  confluence: runbooks (links)
Workflows available: {workflows}
Grid is 12 columns. Respond ONLY with JSON:
{{"title": "...", "components": [{{"id","type","title","col","row","w","h","props"}}]}}
props for data components: {{"datasource": "...", "query": "..."}};
for workflow_button: {{"workflow_id": "..."}}."""


def rebuild_from_prompt(store: Store, user_id: str, prompt: str) -> dict | None:
    """User asked the chat dock to rebuild the dashboard. Returns a proposal."""
    spec = store.get_active_layout(user_id) or {}
    workflows = store.list_workflows(user_id)
    new_spec = None
    if BRIDGE.live:
        out = BRIDGE.json_task(
            f"Current layout: {json.dumps([{k: c[k] for k in ('id','type','title')} for c in spec.get('components', [])])}\n"
            f"User request: {prompt}",
            system=_REBUILD_SYSTEM.format(
                workflows=json.dumps([{ "id": w["id"], "name": w["name"]} for w in workflows])),
        )
        if isinstance(out, dict) and out.get("components"):
            valid = [c for c in out["components"]
                     if c.get("type") in COMPONENT_LIBRARY and c.get("id")]
            # repair workflow bindings the LLM dropped/invented
            wf_ids = {w["id"] for w in workflows}
            old_by_id = {c["id"]: c for c in spec.get("components", [])}
            repaired = []
            for c in valid:
                if c["type"] in ("workflow_button", "workflow_panel"):
                    props = c.setdefault("props", {})
                    if props.get("workflow_id") not in wf_ids:
                        old = old_by_id.get(c["id"], {})
                        old_wf = (old.get("props") or {}).get("workflow_id")
                        if old_wf in wf_ids:
                            props["workflow_id"] = old_wf
                            if old.get("props", {}).get("inputs"):
                                props.setdefault("inputs", old["props"]["inputs"])
                        else:
                            continue  # unfixable — drop rather than render broken
                repaired.append(c)
            valid = repaired
            if valid:
                new_spec = {
                    "title": out.get("title", spec.get("title", "Mission Control")),
                    "user_id": user_id,
                    "grid": {"columns": 12},
                    "components": valid,
                    "chat_dock": spec.get("chat_dock", {"position": "bottom", "visible": True}),
                }
    if new_spec is None:
        # Offline: interpret a few keywords, else reorder around the request.
        new_spec = _offline_rebuild(spec, prompt)

    mutations = [{"op": "replace_spec", "spec": new_spec}]
    summary = f"Rebuild requested via chat: \"{prompt[:60]}\""
    return store.create_proposal(user_id, summary=summary, mutations=mutations,
                                 rationale=f"User asked: {prompt}", engine="rebuild")


def _offline_rebuild(spec: dict, prompt: str) -> dict:
    import copy
    new_spec = copy.deepcopy({k: v for k, v in spec.items() if k != "_meta"})
    low = prompt.lower()
    comps = new_spec.get("components", [])
    def boost(pred):
        hits = [c for c in comps if pred(c)]
        rest = [c for c in comps if not pred(c)]
        return hits + rest
    if "incident" in low or "ops" in low:
        comps = boost(lambda c: "incident" in c["title"].lower() or c["type"] == "workflow_button")
    elif "growth" in low or "business" in low or "signup" in low:
        comps = boost(lambda c: c["props"].get("datasource") == "metabase" or c["type"] == "metric")
    elif "minimal" in low or "clean" in low:
        for c in comps:
            if c["type"] in ("quick_links", "notes", "evolution_log"):
                c["hidden"] = True
    new_spec["components"] = comps
    return new_spec
