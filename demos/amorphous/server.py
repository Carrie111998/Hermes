"""FastAPI server for the Amorphous Applications PoC.

Endpoints:
  GET  /                      dashboard UI
  GET  /api/state             layout + workflows + proposals + agent info
  POST /api/telemetry         record interaction events (batched)
  GET  /api/component/{id}/data   resolve a component's datasource query
  POST /api/workflow/{id}/run     execute a workflow via the agent bridge
  POST /api/layout            save a user-edited layout (move/hide/resize)
  POST /api/curator/run       force a curator pass now (demo button)
  POST /api/proposal/{id}     approve/reject with optional feedback
  POST /api/chat              chat with the agent (also handles /rebuild ...)
  GET  /api/activity          agent + workflow activity feed
"""

from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import datasources
from agent_bridge import BRIDGE
from components import apply_mutations, seed_layout, SEED_WORKFLOWS, COMPONENT_LIBRARY
from curator import rebuild_from_prompt, run_curator
from store import Store

HERE = Path(__file__).parent
DEFAULT_USER = "demo"

app = FastAPI(title="Hermes Station (Amorphous Applications)", version="0.2.0")
store: Store = None  # type: ignore  # set in create_app/main
_curator_state = {"last_run": time.time(), "interval_s": 6 * 3600, "runs": 0}
_chat_histories: dict[str, list[dict]] = {}


def _ensure_user(user_id: str) -> dict:
    spec = store.get_active_layout(user_id)
    if spec is None:
        for wf in SEED_WORKFLOWS:
            store.create_workflow(user_id, wf["name"], wf["prompt_template"],
                                  wf["description"], created_by="seed",
                                  wf_id=wf["id"])
        store.save_layout(user_id, seed_layout(user_id), source="seed")
        spec = store.get_active_layout(user_id)
    assert spec is not None
    return spec


# ---------- models ----------
class TelemetryBatch(BaseModel):
    user_id: str = DEFAULT_USER
    events: list[dict]


class LayoutSave(BaseModel):
    user_id: str = DEFAULT_USER
    spec: dict


class ProposalAction(BaseModel):
    action: str  # approve | reject
    feedback: str = ""
    sentiment: str = ""  # up | down


class ChatMessage(BaseModel):
    user_id: str = DEFAULT_USER
    text: str


class WorkflowRunRequest(BaseModel):
    user_id: str = DEFAULT_USER
    inputs: dict = {}


# ---------- routes ----------
@app.get("/api/state")
def get_state(user_id: str = DEFAULT_USER):
    spec = _ensure_user(user_id)
    return {
        "layout": spec,
        "workflows": store.list_workflows(user_id),
        "proposals": store.list_proposals(user_id, status="pending"),
        "history": store.layout_history(user_id),
        "agent": BRIDGE.describe(),
        "datasources": datasources.statuses(),
        "curator": {
            "interval_s": _curator_state["interval_s"],
            "last_run": _curator_state["last_run"],
            "runs": _curator_state["runs"],
        },
        "library": COMPONENT_LIBRARY,
    }


@app.post("/api/telemetry")
def post_telemetry(batch: TelemetryBatch):
    for ev in batch.events[:500]:
        etype = str(ev.get("type", ""))[:32]
        if not etype:
            continue
        store.record_event(batch.user_id, etype, ev.get("component_id"),
                           ev.get("payload"))
    return {"ok": True, "recorded": len(batch.events)}


@app.get("/api/component/{component_id}/data")
def component_data(component_id: str, user_id: str = DEFAULT_USER,
                   proposal_id: str = ""):
    if proposal_id:
        # preview mode: resolve the component from the proposal's would-be spec
        p = store.get_proposal(proposal_id)
        if not p:
            raise HTTPException(404, "proposal not found")
        spec = _proposal_preview_spec(user_id, p)
    else:
        spec = _ensure_user(user_id)
    comp = next((c for c in spec["components"] if c["id"] == component_id), None)
    if not comp:
        raise HTTPException(404, "component not found")
    ctype = comp["type"]
    props = comp.get("props", {})
    if ctype in ("metric", "timeseries", "table", "quick_links"):
        ds, q = props.get("datasource"), props.get("query")
        if ds and q:
            return datasources.query(ds, q)
        return {"kind": "error", "error": "component has no datasource binding"}
    if ctype == "datasource_status":
        return {"kind": "statuses", "statuses": datasources.statuses()}
    if ctype == "notes":
        return {"kind": "notes", "markdown": props.get("markdown", "")}
    if ctype in ("workflow_button", "workflow_panel"):
        wf_id = props.get("workflow_id", "")
        wf = store.get_workflow(wf_id)
        runs = store.workflow_runs(wf_id, limit=3) if wf else []
        return {"kind": "workflow", "workflow": wf, "runs": runs}
    if ctype == "agent_activity":
        return {"kind": "activity", "items": _activity(user_id)}
    if ctype == "evolution_log":
        items = [
            {"when": p["created_at"], "status": p["status"], "summary": p["summary"],
             "engine": p["engine"]}
            for p in store.list_proposals(user_id)
        ]
        return {"kind": "evolution", "items": items}
    return {"kind": "error", "error": f"no data handler for {ctype}"}


def _activity(user_id: str) -> list[dict]:
    items = []
    for wf in store.list_workflows(user_id):
        for run in store.workflow_runs(wf["id"], limit=3):
            items.append({"ts": run["ts"], "kind": "workflow",
                          "text": f"{wf['name']} → {run['status']}"})
    for ev in store.events_since(user_id, time.time() - 24 * 3600):
        if ev["type"] == "chat":
            items.append({"ts": ev["ts"], "kind": "chat",
                          "text": (ev.get("payload") or {}).get("text", "")[:80]})
    items.sort(key=lambda i: -i["ts"])
    return items[:15]


@app.post("/api/workflow/{wf_id}/run")
def run_workflow(wf_id: str, req: WorkflowRunRequest):
    wf = store.get_workflow(wf_id)
    if not wf:
        raise HTTPException(404, "workflow not found")
    context = {
        "incidents": datasources.query("betterstack", "incidents.open"),
        "error_rate": datasources.query("datadog", "error.rate"),
        "recent_activity": _activity(req.user_id)[:5],
    }
    try:
        prompt = wf["prompt_template"].format(context=context, **req.inputs)
    except KeyError as e:
        raise HTTPException(400, f"missing workflow input: {e}")
    store.record_event(req.user_id, "workflow_run", None, {"workflow_id": wf_id})
    try:
        result = BRIDGE.chat(
            [{"role": "user", "content": prompt}],
            system="You are Hermes running a saved dashboard workflow. Be concise "
                   "and actionable; plain text.")
        run = store.record_workflow_run(wf_id, req.user_id, "ok", prompt, result)
    except Exception as e:
        run = store.record_workflow_run(wf_id, req.user_id, "error", prompt, str(e))
    return run


@app.post("/api/layout")
def save_layout(body: LayoutSave):
    version = store.save_layout(body.user_id, body.spec, source="user")
    return {"ok": True, "version": version}


@app.post("/api/curator/run")
def curator_now(user_id: str = DEFAULT_USER):
    since = _curator_state["last_run"] - _curator_state["interval_s"]
    proposal = run_curator(store, user_id, since_ts=since)
    _curator_state["last_run"] = time.time()
    _curator_state["runs"] += 1
    return {"ok": True, "proposal": proposal}


def _proposal_preview_spec(user_id: str, p: dict) -> dict:
    """The layout as it WOULD look if this proposal were approved."""
    replace = next((m for m in p["mutations"] if m.get("op") == "replace_spec"), None)
    if replace:
        return replace["spec"]
    return apply_mutations(_ensure_user(user_id), p["mutations"])


@app.get("/api/proposal/{pid}/preview")
def preview_proposal(pid: str):
    """Try-before-you-approve: returns the evolved layout without applying it,
    plus a component-level diff versus the current dashboard."""
    p = store.get_proposal(pid)
    if not p:
        raise HTTPException(404, "proposal not found")
    user_id = p["user_id"]
    current = _ensure_user(user_id)
    preview = _proposal_preview_spec(user_id, p)
    store.record_event(user_id, "proposal_action", None,
                       {"proposal_id": pid, "action": "preview"})

    cur = {c["id"]: c for c in current.get("components", [])}
    new = {c["id"]: c for c in preview.get("components", [])}
    diff = []
    for cid, c in new.items():
        old = cur.get(cid)
        if old is None:
            diff.append({"id": cid, "change": "added", "title": c["title"]})
        elif old.get("hidden") != c.get("hidden"):
            diff.append({"id": cid, "change": "hidden" if c.get("hidden") else "shown",
                         "title": c["title"]})
        elif (old.get("w"), old.get("h")) != (c.get("w"), c.get("h")):
            diff.append({"id": cid, "change": "resized", "title": c["title"]})
        elif old.get("title") != c.get("title"):
            diff.append({"id": cid, "change": "renamed",
                         "title": f"{old['title']} → {c['title']}"})
    for cid, c in cur.items():
        if cid not in new:
            diff.append({"id": cid, "change": "removed", "title": c["title"]})
    return {"proposal": p, "preview": preview, "diff": diff}


@app.post("/api/proposal/{pid}")
def act_on_proposal(pid: str, body: ProposalAction):
    p = store.get_proposal(pid)
    if not p:
        raise HTTPException(404, "proposal not found")
    if p["status"] != "pending":
        raise HTTPException(409, f"proposal already {p['status']}")
    user_id = p["user_id"]
    store.record_event(user_id, "proposal_action", None,
                       {"proposal_id": pid, "action": body.action})
    if body.sentiment or body.feedback:
        store.add_feedback(user_id, pid, body.sentiment or "neutral", body.feedback)
    if body.action == "approve":
        spec = _ensure_user(user_id)
        replace = next((m for m in p["mutations"] if m.get("op") == "replace_spec"), None)
        if replace:
            new_spec = replace["spec"]
        else:
            new_spec = apply_mutations(spec, p["mutations"])
        version = store.save_layout(user_id, new_spec,
                                    source="curator" if p["engine"] != "rebuild" else "rebuild")
        store.resolve_proposal(pid, "approved", body.feedback)
        return {"ok": True, "applied": True, "version": version}
    store.resolve_proposal(pid, "rejected", body.feedback)
    return {"ok": True, "applied": False}


_CHAT_SYSTEM = """You are Hermes, embedded in the user's Amorphous mission-control dashboard.
You can see their dashboard and help them operate it. Special abilities you can mention:
- If they want the dashboard restructured, they can type: /rebuild <what they want>
- Workflow shortcuts on the dashboard run saved agent tasks with one click.
Keep answers short and operational."""


@app.post("/api/chat")
def chat(body: ChatMessage):
    user_id, text = body.user_id, body.text.strip()
    if not text:
        raise HTTPException(400, "empty message")
    store.record_event(user_id, "chat", None, {"text": text})

    if text.lower().startswith("/rebuild"):
        req = text[len("/rebuild"):].strip() or "rebuild this dashboard for my current needs"
        proposal = rebuild_from_prompt(store, user_id, req)
        return {"reply": "I drafted a rebuilt dashboard for that. Review it in the "
                         "proposals tray (top right) and approve to apply.",
                "proposal": proposal}
    if text.lower().startswith("/evolve"):
        proposal = run_curator(store, user_id,
                               since_ts=time.time() - _curator_state["interval_s"])
        if proposal:
            return {"reply": "Curator pass complete — a proposal is waiting in the tray.",
                    "proposal": proposal}
        return {"reply": "Curator pass complete — nothing worth changing yet. "
                         "Interact with the dashboard a bit more.", "proposal": None}

    hist = _chat_histories.setdefault(user_id, [])
    hist.append({"role": "user", "content": text})
    spec = _ensure_user(user_id)
    ctx = (f"\n\nDashboard context: components="
           f"{[{c['id']: c['title']} for c in spec['components'] if not c.get('hidden')]}")
    reply = BRIDGE.chat(hist[-12:], system=_CHAT_SYSTEM + ctx)
    hist.append({"role": "assistant", "content": reply})
    return {"reply": reply, "proposal": None}


@app.get("/api/activity")
def activity(user_id: str = DEFAULT_USER):
    return {"items": _activity(user_id)}


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")


def _curator_loop():
    while True:
        time.sleep(30)
        if time.time() - _curator_state["last_run"] >= _curator_state["interval_s"]:
            try:
                run_curator(store, DEFAULT_USER,
                            since_ts=_curator_state["last_run"])
            except Exception:
                pass
            _curator_state["last_run"] = time.time()
            _curator_state["runs"] += 1


def create_app(db_path: str | Path, curator_interval_s: int = 6 * 3600) -> FastAPI:
    global store
    store = Store(db_path)
    _curator_state["interval_s"] = curator_interval_s
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    return app


def main() -> None:
    import uvicorn
    ap = argparse.ArgumentParser(description="Amorphous Applications PoC server")
    ap.add_argument("--port", type=int, default=8877)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--db", default=str(HERE / "amorphous.db"))
    ap.add_argument("--curator-minutes", type=float, default=360,
                    help="curator interval in minutes (demo: try 1)")
    args = ap.parse_args()
    create_app(args.db, curator_interval_s=int(args.curator_minutes * 60))
    threading.Thread(target=_curator_loop, daemon=True).start()
    print(f"Hermes Station → http://{args.host}:{args.port}  "
          f"(agent: {BRIDGE.describe()['model']})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
