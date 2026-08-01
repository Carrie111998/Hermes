"""Hermes Station server (Amorphous Applications PoC v2).

Key surfaces:
  /onboarding      first-run: templates, connection detection, open-ended brief
  /api/state       layout + workflows + proposals + agent + connections
  /api/chat        main dock — FULL AIAgent, dashboard-wide station tools
  /api/component/{id}/chat   component-scoped agent chat
  /api/component/{id}/data   live data resolution
  /api/workflow/{id}/run     real agent workflow execution
  curator loop + proposals + preview + rejection memory (unchanged core)
"""

from __future__ import annotations

import argparse
import json
import queue
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import datasources
from agent_bridge import BRIDGE, StationAgent, set_station_context
from components import (COMPONENT_LIBRARY, TEMPLATES, TEMPLATE_WORKFLOWS,
                        apply_mutations, seed_layout)
from curator import rebuild_from_prompt, run_curator
from store import Store

HERE = Path(__file__).parent
DEFAULT_USER = "demo"

app = FastAPI(title="Hermes Station", version="0.3.0")
store: Store = None  # type: ignore
_curator_state = {"last_run": time.time(), "interval_s": 6 * 3600, "runs": 0}
_agents: dict[str, StationAgent] = {}
_agents_lock = threading.Lock()
_events: "queue.Queue[dict]" = queue.Queue()


def _agent_for(user_id: str, component_id: str | None = None) -> StationAgent:
    key = f"{user_id}:{component_id or 'main'}"
    with _agents_lock:
        if key not in _agents:
            _agents[key] = StationAgent(
                scope="component" if component_id else "main",
                component_id=component_id)
        return _agents[key]


def _emit(kind: str, **kw):
    try:
        _events.put_nowait({"kind": kind, "ts": time.time(), **kw})
    except Exception:
        pass


# ---------- models ----------
class TelemetryBatch(BaseModel):
    user_id: str = DEFAULT_USER
    events: list[dict]


class LayoutSave(BaseModel):
    user_id: str = DEFAULT_USER
    spec: dict


class ProposalAction(BaseModel):
    action: str
    feedback: str = ""
    sentiment: str = ""


class ChatMessage(BaseModel):
    user_id: str = DEFAULT_USER
    text: str


class WorkflowRunRequest(BaseModel):
    user_id: str = DEFAULT_USER
    inputs: dict = {}


class OnboardRequest(BaseModel):
    user_id: str = DEFAULT_USER
    template: str = "developer"
    repo: str = ""
    brief: str = ""


# ---------- onboarding ----------
@app.get("/api/onboarding/options")
def onboarding_options():
    return {
        "templates": [{"id": k, "name": v["name"], "blurb": v["blurb"]}
                      for k, v in TEMPLATES.items()],
        "connections": datasources.detect_connections(),
        "agent": BRIDGE.describe(),
    }


@app.post("/api/onboarding/complete")
def onboarding_complete(req: OnboardRequest):
    if store.get_active_layout(req.user_id):
        raise HTTPException(409, "user already onboarded")
    kw = {}
    if req.template == "developer":
        kw["repo"] = req.repo or str(Path.home() / ".hermes" / "hermes-agent")
    spec = seed_layout(req.user_id, template=req.template, **kw)
    for wf in TEMPLATE_WORKFLOWS.get(req.template, []):
        tmpl = wf["prompt_template"]
        if req.template == "developer":
            tmpl = tmpl.replace("{repo}", kw.get("repo", "."))
        store.create_workflow(req.user_id, wf["name"], tmpl,
                              wf["description"], created_by="seed", wf_id=wf["id"])
    version = store.save_layout(req.user_id, spec, source="seed")
    store.record_event(req.user_id, "onboarded", None,
                       {"template": req.template, "brief": req.brief})
    # Open-ended brief → hand to the main agent to customize the fresh board
    reply = ""
    if req.brief.strip():
        agent = _agent_for(req.user_id)
        reply = agent.chat(
            store, req.user_id,
            f"I just onboarded with the '{req.template}' template. Customize my "
            f"dashboard for this brief (use station_mutate; verify data sources "
            f"with station_query_datasource first): {req.brief}",
            on_mutation=lambda: _emit("layout_changed", user_id=req.user_id))
    return {"ok": True, "version": version, "agent_reply": reply}


# ---------- state ----------
@app.get("/api/state")
def get_state(user_id: str = DEFAULT_USER):
    spec = store.get_active_layout(user_id)
    if spec is None:
        return {"onboarded": False, "agent": BRIDGE.describe()}
    return {
        "onboarded": True,
        "layout": spec,
        "workflows": store.list_workflows(user_id),
        "proposals": store.list_proposals(user_id, status="pending"),
        "history": store.layout_history(user_id),
        "agent": _agent_for(user_id).describe(),
        "connections": datasources.detect_connections(),
        "curator": {"interval_s": _curator_state["interval_s"],
                    "last_run": _curator_state["last_run"],
                    "runs": _curator_state["runs"]},
        "library": COMPONENT_LIBRARY,
    }


@app.post("/api/telemetry")
def post_telemetry(batch: TelemetryBatch):
    for ev in batch.events[:500]:
        etype = str(ev.get("type", ""))[:32]
        if etype:
            store.record_event(batch.user_id, etype, ev.get("component_id"),
                               ev.get("payload"))
    return {"ok": True}


# ---------- component data ----------
def _resolve_component(spec: dict, component_id: str) -> dict:
    comp = next((c for c in spec.get("components", []) if c["id"] == component_id), None)
    if not comp:
        raise HTTPException(404, "component not found")
    return comp


def _component_payload(comp: dict, user_id: str) -> dict:
    ctype = comp["type"]
    props = comp.get("props", {})
    if props.get("source"):
        return datasources.query(props["source"], props.get("query", {}),
                                 store=store, user_id=user_id)
    if ctype == "notes":
        return {"kind": "notes", "markdown": props.get("markdown", "")}
    if ctype == "connections":
        return {"kind": "connections", "connections": datasources.detect_connections()}
    if ctype in ("workflow_button", "workflow_panel"):
        wf_id = props.get("workflow_id", "")
        wf = store.get_workflow(wf_id)
        runs = store.workflow_runs(wf_id, limit=3) if wf else []
        return {"kind": "workflow", "workflow": wf, "runs": runs}
    return {"kind": "error", "error": f"component has no data binding ({ctype})"}


@app.get("/api/component/{component_id}/data")
def component_data(component_id: str, user_id: str = DEFAULT_USER,
                   proposal_id: str = ""):
    if proposal_id:
        p = store.get_proposal(proposal_id)
        if not p:
            raise HTTPException(404, "proposal not found")
        spec = _proposal_preview_spec(user_id, p)
    else:
        spec = store.get_active_layout(user_id) or {}
    comp = _resolve_component(spec, component_id)
    return _component_payload(comp, user_id)


@app.post("/api/component/{component_id}/chat")
def component_chat(component_id: str, body: ChatMessage):
    spec = store.get_active_layout(body.user_id) or {}
    comp = _resolve_component(spec, component_id)
    data = _component_payload(comp, body.user_id)
    store.record_event(body.user_id, "component_chat", component_id,
                       {"text": body.text})
    agent = _agent_for(body.user_id, component_id)
    note = (f"[Component context] id={component_id} "
            f"definition={json.dumps({k: comp.get(k) for k in ('type','title','w','h','props')})} "
            f"current_data={json.dumps(data)[:3000]}")
    reply = agent.chat(store, body.user_id, body.text, context_note=note,
                       on_mutation=lambda: _emit("layout_changed", user_id=body.user_id),
                       tool_event=lambda name, args: _emit("tool", scope=component_id, name=name))
    return {"reply": reply}


# ---------- workflows ----------
@app.post("/api/workflow/{wf_id}/run")
def run_workflow(wf_id: str, req: WorkflowRunRequest):
    wf = store.get_workflow(wf_id)
    if not wf:
        raise HTTPException(404, "workflow not found")
    spec = store.get_active_layout(req.user_id) or {}
    context = {
        "dashboard_components": [{c['id']: c['title']} for c in spec.get("components", [])],
        "recent_activity": [e["type"] for e in store.events_since(req.user_id, time.time() - 3600)][-10:],
    }
    try:
        prompt = wf["prompt_template"].format(context=json.dumps(context), **req.inputs)
    except KeyError as e:
        raise HTTPException(400, f"missing workflow input: {e}")
    store.record_event(req.user_id, "workflow_run", None, {"workflow_id": wf_id})
    agent = _agent_for(req.user_id)  # full agent: terminal, web, station tools
    try:
        result = agent.chat(store, req.user_id,
                            f"[Workflow: {wf['name']}] {prompt}",
                            on_mutation=lambda: _emit("layout_changed", user_id=req.user_id),
                            tool_event=lambda name, args: _emit("tool", scope="workflow", name=name))
        run = store.record_workflow_run(wf_id, req.user_id, "ok", prompt, result)
    except Exception as e:
        run = store.record_workflow_run(wf_id, req.user_id, "error", prompt, str(e))
    return run


# ---------- layout (user drag/resize/hide — saved VERBATIM, no repack) ----------
@app.post("/api/layout")
def save_layout(body: LayoutSave):
    spec = {k: v for k, v in body.spec.items() if k != "_meta"}
    version = store.save_layout(body.user_id, spec, source="user")
    return {"ok": True, "version": version}


# ---------- curator / proposals ----------
@app.post("/api/curator/run")
def curator_now(user_id: str = DEFAULT_USER):
    since = _curator_state["last_run"] - _curator_state["interval_s"]
    proposal = run_curator(store, user_id, since_ts=since)
    _curator_state["last_run"] = time.time()
    _curator_state["runs"] += 1
    if proposal:
        _emit("proposal", user_id=user_id)
    return {"ok": True, "proposal": proposal}


def _proposal_preview_spec(user_id: str, p: dict) -> dict:
    replace = next((m for m in p["mutations"] if m.get("op") == "replace_spec"), None)
    if replace:
        return replace["spec"]
    return apply_mutations(store.get_active_layout(user_id) or {}, p["mutations"])


@app.get("/api/proposal/{pid}/preview")
def preview_proposal(pid: str):
    p = store.get_proposal(pid)
    if not p:
        raise HTTPException(404, "proposal not found")
    user_id = p["user_id"]
    current = store.get_active_layout(user_id) or {}
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
        new_spec = _proposal_preview_spec(user_id, p)
        version = store.save_layout(
            user_id, new_spec,
            source="curator" if p["engine"] != "rebuild" else "rebuild")
        store.resolve_proposal(pid, "approved", body.feedback)
        _emit("layout_changed", user_id=user_id)
        return {"ok": True, "applied": True, "version": version}
    store.resolve_proposal(pid, "rejected", body.feedback)
    return {"ok": True, "applied": False}


# ---------- main chat ----------
@app.post("/api/chat")
def chat(body: ChatMessage):
    user_id, text = body.user_id, body.text.strip()
    if not text:
        raise HTTPException(400, "empty message")
    store.record_event(user_id, "chat", None, {"text": text})

    if text.lower().startswith("/rebuild"):
        req = text[len("/rebuild"):].strip() or "rebuild for my current needs"
        proposal = rebuild_from_prompt(store, user_id, req)
        _emit("proposal", user_id=user_id)
        return {"reply": "Drafted a rebuild — preview it in the ▣ tray.",
                "proposal": proposal}
    if text.lower().startswith("/evolve"):
        proposal = run_curator(store, user_id,
                               since_ts=time.time() - _curator_state["interval_s"])
        if proposal:
            _emit("proposal", user_id=user_id)
            return {"reply": "Curator proposal waiting in the tray.", "proposal": proposal}
        return {"reply": "Nothing worth changing yet — keep using the dashboard.",
                "proposal": None}

    agent = _agent_for(user_id)
    spec = store.get_active_layout(user_id) or {}
    slim = [{c["id"]: c["title"]} for c in spec.get("components", []) if not c.get("hidden")]
    note = f"[Dashboard snapshot] visible components: {json.dumps(slim)}"
    reply = agent.chat(store, user_id, text, context_note=note,
                       on_mutation=lambda: _emit("layout_changed", user_id=user_id),
                       tool_event=lambda name, args: _emit("tool", scope="main", name=name))
    return {"reply": reply, "proposal": None}


# ---------- SSE events (live updates) ----------
@app.get("/api/events")
def sse_events():
    def gen():
        yield "retry: 3000\n\n"
        while True:
            try:
                ev = _events.get(timeout=25)
                yield f"data: {json.dumps(ev)}\n\n"
            except queue.Empty:
                yield ": keepalive\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------- pages ----------
_NO_CACHE = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}
_DIST = HERE / "web" / "dist"


@app.get("/")
def index(user: str = DEFAULT_USER):
    spa = _DIST / "index.html"
    if spa.exists():
        return FileResponse(spa, headers=_NO_CACHE)
    if store.get_active_layout(user) is None:
        return RedirectResponse(f"/onboarding?user={user}", headers=_NO_CACHE)
    return FileResponse(HERE / "static" / "index.html", headers=_NO_CACHE)


@app.get("/onboarding")
def onboarding_page():
    spa = _DIST / "index.html"
    if spa.exists():
        return FileResponse(spa, headers=_NO_CACHE)
    return FileResponse(HERE / "static" / "onboarding.html", headers=_NO_CACHE)


def _curator_loop():
    while True:
        time.sleep(30)
        if time.time() - _curator_state["last_run"] >= _curator_state["interval_s"]:
            try:
                p = run_curator(store, DEFAULT_USER, since_ts=_curator_state["last_run"])
                if p:
                    _emit("proposal", user_id=DEFAULT_USER)
            except Exception:
                pass
            _curator_state["last_run"] = time.time()
            _curator_state["runs"] += 1


def create_app(db_path: str | Path, curator_interval_s: int = 6 * 3600) -> FastAPI:
    global store
    store = Store(db_path)
    _curator_state["interval_s"] = curator_interval_s
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    if (_DIST / "assets").exists():
        app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")
    return app


def main() -> None:
    import uvicorn
    ap = argparse.ArgumentParser(description="Hermes Station")
    ap.add_argument("--port", type=int, default=8877)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--db", default=str(HERE / "amorphous.db"))
    ap.add_argument("--curator-minutes", type=float, default=360)
    args = ap.parse_args()
    create_app(args.db, curator_interval_s=int(args.curator_minutes * 60))
    threading.Thread(target=_curator_loop, daemon=True).start()
    print(f"Hermes Station → http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
