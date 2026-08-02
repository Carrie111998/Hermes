"""Hermes Station agent bridge — the REAL AIAgent, not an HTTP shim.

Two session scopes, both full agents with the user's configured model/provider
and toolsets:

  main dock   — sees the whole dashboard (spec snapshot injected per turn) and
                carries station_* tools that can mutate the entire layout.
  component   — scoped to one component; its station tools only touch that
                component (enforced server-side, not by prompt).

Station tools are registered into the live tool registry at import time
(AFTER model_tools discovery) and added to a `station` toolset + core-tool
exemption so tool_search never defers them.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[2]
if str(WORKTREE) not in sys.path:
    sys.path.insert(0, str(WORKTREE))

import model_tools  # noqa: E402  (triggers tool discovery first)
import toolsets  # noqa: E402
from tools.registry import registry  # noqa: E402
from hermes_cli.runtime_provider import resolve_runtime_provider  # noqa: E402
from cli import load_cli_config  # noqa: E402
from run_agent import AIAgent  # noqa: E402

# ---------------------------------------------------------------- context
# The station server sets this before each agent turn so station tools know
# which store/user/component they operate on. Thread-local because uvicorn
# may serve concurrent chats.
_ctx = threading.local()


def set_station_context(store, user_id: str, component_id: str | None = None,
                        on_mutation=None):
    _ctx.store = store
    _ctx.user_id = user_id
    _ctx.component_id = component_id
    _ctx.on_mutation = on_mutation


def _require_ctx():
    store = getattr(_ctx, "store", None)
    if store is None:
        raise RuntimeError("station context not set")
    return store, _ctx.user_id, getattr(_ctx, "component_id", None)


# ---------------------------------------------------------------- station tools

def _tool_station_get_dashboard(args, **kw):
    store, user_id, comp_id = _require_ctx()
    spec = store.get_active_layout(user_id) or {}
    if comp_id:
        comp = next((c for c in spec.get("components", []) if c["id"] == comp_id), None)
        return json.dumps({"scope": "component", "component": comp})
    slim = [{k: c.get(k) for k in ("id", "type", "title", "w", "h", "hidden", "props")}
            for c in spec.get("components", [])]
    return json.dumps({"scope": "dashboard", "title": spec.get("title"),
                       "components": slim,
                       "workflows": [{"id": w["id"], "name": w["name"]}
                                     for w in store.list_workflows(user_id)]})


def _tool_station_mutate(args, **kw):
    """Apply layout mutations. Component-scoped sessions may only touch their
    own component; dashboard scope can do anything."""
    from components import apply_mutations, COMPONENT_LIBRARY
    store, user_id, comp_id = _require_ctx()
    muts = args.get("mutations") or []
    if not isinstance(muts, list) or not muts:
        return json.dumps({"error": "mutations must be a non-empty list"})
    if comp_id:
        for m in muts:
            if m.get("op") == "add":
                return json.dumps({"error": "component-scoped session cannot add components; ask the main chat"})
            if m.get("component_id") != comp_id:
                return json.dumps({"error": f"this session is scoped to {comp_id} only"})
    bad = [m.get("op") for m in muts if m.get("op") not in
           ("promote", "shrink", "hide", "show", "remove", "retitle", "add",
            "set_props", "set_notes", "move_chat_dock", "resize")]
    if bad:
        return json.dumps({"error": f"unknown ops: {bad}"})
    spec = store.get_active_layout(user_id)
    new_spec = apply_mutations(spec, muts)
    version = store.save_layout(user_id, new_spec,
                                source="agent" if not comp_id else "component-agent")
    cb = getattr(_ctx, "on_mutation", None)
    if cb:
        try:
            cb()
        except Exception:
            pass
    return json.dumps({"ok": True, "version": version, "applied": len(muts)})


def _tool_station_query_datasource(args, **kw):
    import datasources
    store, user_id, _ = _require_ctx()
    src = args.get("source", "")
    props = args.get("props") or {}
    out = datasources.query(src, props, store=store, user_id=user_id)
    return json.dumps(out)[:8000]


def _tool_station_create_workflow(args, **kw):
    store, user_id, comp_id = _require_ctx()
    if comp_id:
        return json.dumps({"error": "component-scoped session cannot create workflows"})
    name = args.get("name", "").strip()
    template = args.get("prompt_template", "").strip()
    if not name or not template:
        return json.dumps({"error": "name and prompt_template required"})
    wf = store.create_workflow(user_id, name=name, prompt_template=template,
                               description=args.get("description", ""),
                               created_by="agent")
    return json.dumps({"ok": True, "workflow": wf})


def _tool_station_component_data(args, **kw):
    """Current rendered data of a component (what the user is looking at)."""
    import datasources
    store, user_id, _ = _require_ctx()
    cid = args.get("component_id", "")
    spec = store.get_active_layout(user_id) or {}
    comp = next((c for c in spec.get("components", []) if c["id"] == cid), None)
    if not comp:
        return json.dumps({"error": f"no component {cid}"})
    props = comp.get("props", {})
    if props.get("source"):
        data = datasources.query(props["source"], props.get("query", {}) or props,
                                 store=store, user_id=user_id)
    elif comp["type"] == "notes":
        data = {"kind": "notes", "markdown": props.get("markdown", "")}
    else:
        data = {"kind": comp["type"]}
    return json.dumps({"component": {"id": cid, "type": comp["type"],
                                     "title": comp["title"], "props": props},
                       "data": data})[:8000]


_STATION_TOOLS = [
    ("station_get_dashboard",
     "Read the current Hermes Station dashboard layout (components, workflows). "
     "In a component-scoped chat, returns just that component.",
     {"type": "object", "properties": {}},
     _tool_station_get_dashboard),
    ("station_mutate",
     "Mutate the Station dashboard layout. mutations: list of "
     "{op, component_id?, ...}. Ops: promote, shrink, resize {w,h}, hide, show, "
     "remove, retitle {title}, set_props {props}, set_notes {markdown}, "
     "add {component:{id,type,title,w,h,props}}, move_chat_dock {position}. "
     "Component types: metric,timeseries,table,kv,feed,links,workflow_button,"
     "workflow_panel,notes,connections,heatmap,logs,tasklist. "
     "tasklist props: {items:[{text,done}]} — agent-editable via set_props "
     "(great for tracking work you're doing for the user). "
     "workflow_button/workflow_panel props take inputs:[FieldSpec] where "
     "FieldSpec = {name, label?, type?: text|textarea|number|select|switch|"
     "slider|date|password, required?, placeholder?, default?, "
     "options?:[str|{value,label}], min?, max?, step?, rows?}. Every {name} "
     "placeholder in the workflow's prompt_template MUST have a matching "
     "input spec (the UI auto-derives text/number fields for undeclared "
     "placeholders, but explicit specs give better controls — e.g. a select "
     "of environments, a switch for dry-run, a slider for limits). "
     "Data components take props "
     "{source, query:{...}} — sources: git.log{repo,limit}, git.status{repo}, "
     "github.prs{repo?,limit}, github.issues{repo?}, system.stats{}, "
     "crypto.price{coins}, crypto.chart{coin}, rss{url,limit}, weather{lat,lon}, "
     "datadog.query{query}, betterstack.monitors{}, station.activity{}, "
     "git.heatmap{repo,weeks} (commit calendar), log.tail{path,lines} (live file tail, 5s). "
     "Changes apply IMMEDIATELY (the user watches live) — no approval loop for "
     "chat-driven edits.",
     {"type": "object", "properties": {
         "mutations": {"type": "array", "items": {"type": "object"}}},
      "required": ["mutations"]},
     _tool_station_mutate),
    ("station_query_datasource",
     "Run a Station datasource query and see the raw result (same sources as "
     "station_mutate props). Use to verify data before wiring a component.",
     {"type": "object", "properties": {
         "source": {"type": "string"}, "props": {"type": "object"}},
      "required": ["source"]},
     _tool_station_query_datasource),
    ("station_create_workflow",
     "Create a reusable Station workflow (agent task template). Use {context} "
     "placeholder for dashboard context injection; other {name} placeholders "
     "become component input fields.",
     {"type": "object", "properties": {
         "name": {"type": "string"}, "prompt_template": {"type": "string"},
         "description": {"type": "string"}},
      "required": ["name", "prompt_template"]},
     _tool_station_create_workflow),
    ("station_component_data",
     "Fetch the live rendered data of one dashboard component by id — exactly "
     "what the user currently sees in that card.",
     {"type": "object", "properties": {"component_id": {"type": "string"}},
      "required": ["component_id"]},
     _tool_station_component_data),
]

for name, desc, params, handler in _STATION_TOOLS:
    registry.register(
        name=name, toolset="station",
        schema={"name": name, "description": desc, "parameters": params},
        handler=handler,
    )
toolsets.TOOLSETS["station"] = {
    "description": "Hermes Station dashboard control tools",
    "tools": [t[0] for t in _STATION_TOOLS],
    "includes": [],
}
# exempt from tool_search deferral
for t in _STATION_TOOLS:
    if t[0] not in toolsets._HERMES_CORE_TOOLS:
        toolsets._HERMES_CORE_TOOLS.append(t[0])


# ---------------------------------------------------------------- agents

_MAIN_SYSTEM = """You are Hermes, running INSIDE the user's Hermes Station dashboard \
(working codename; concept: Amorphous Applications). You are their primary work \
interface. You have your full toolset (terminal, files, web, etc.) PLUS station_* \
tools that read and reshape the dashboard live.

Behavior:
- When the user asks to change the dashboard (add/remove/resize/rewire components, \
new data, new workflows) — do it directly with station_mutate. They watch it happen.
- Use station_query_datasource to verify a data query works before wiring it in.
- Real data only: never invent numbers; if a source isn't connected, say what's needed.
- Keep replies terse and operational; this is a work surface, not a chat lounge."""

_COMPONENT_SYSTEM = """You are Hermes, scoped to ONE component of the user's Hermes \
Station dashboard. The component's definition and current data are provided. You may \
reconfigure THIS component only (station_mutate with its component_id: retitle, \
resize, set_props to change its query, hide). You cannot add components or touch \
others — direct the user to the main chat for that. Answer questions about the \
component's data using station_component_data / station_query_datasource and your \
full toolset. Terse, operational replies."""


class StationAgent:
    """Wraps one AIAgent conversation (main dock or per-component)."""

    def __init__(self, scope: str = "main", component_id: str | None = None):
        cfg = load_cli_config()
        mc = cfg.get("model", {}) if isinstance(cfg.get("model"), dict) else {}
        rt = resolve_runtime_provider()
        self.model = mc.get("default") or mc.get("model") or rt.get("model") or ""
        self.provider = rt.get("provider")
        self.component_id = component_id
        self.scope = scope
        self._lock = threading.Lock()
        self.agent = AIAgent(
            model=self.model,
            provider=rt.get("provider"),
            api_key=rt.get("api_key"),
            base_url=rt.get("base_url"),
            api_mode=rt.get("api_mode"),
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=(scope != "main"),
            enabled_toolsets=["terminal", "file", "web", "vision", "todo",
                              "skills", "station"],
            max_iterations=40,
            ephemeral_system_prompt=(_MAIN_SYSTEM if scope == "main"
                                     else _COMPONENT_SYSTEM),
        )
        self.history: list[dict] = []

    def describe(self) -> dict:
        return {"live": True, "model": self.model, "provider": self.provider,
                "scope": self.scope}

    def chat(self, store, user_id: str, text: str, context_note: str = "",
             on_mutation=None, tool_event=None) -> str:
        with self._lock:
            set_station_context(store, user_id,
                                component_id=self.component_id,
                                on_mutation=on_mutation)
            if tool_event:
                self.agent.tool_start_callback = (
                    lambda cid, name, targs: tool_event(name, targs))
            msg = (context_note + "\n\n" + text) if context_note else text
            try:
                result = self.agent.run_conversation(
                    msg, conversation_history=self.history)
                self.history = result.get("messages", self.history)
                return result.get("final_response") or "(no response)"
            except Exception as e:
                return f"⚠ agent error: {e}"


# ---------------------------------------------------------------- bridge shim
# Curator + workflow paths use a lightweight one-shot interface backed by the
# same real runtime credentials (no station tools, no history).

class _BridgeShim:
    def __init__(self):
        self._agent = None
        self._lock = threading.Lock()

    @property
    def live(self) -> bool:
        return True

    def _get(self):
        if self._agent is None:
            cfg = load_cli_config()
            mc = cfg.get("model", {}) if isinstance(cfg.get("model"), dict) else {}
            rt = resolve_runtime_provider()
            self._agent = AIAgent(
                model=mc.get("default") or mc.get("model") or "",
                provider=rt.get("provider"), api_key=rt.get("api_key"),
                base_url=rt.get("base_url"), api_mode=rt.get("api_mode"),
                quiet_mode=True, skip_context_files=True, skip_memory=True,
                enabled_toolsets=["todo"], max_iterations=2,
            )
        return self._agent

    def describe(self) -> dict:
        try:
            a = self._get()
            return {"live": True, "model": a.model or "configured default"}
        except Exception as e:
            return {"live": False, "model": f"error: {e}"}

    def chat(self, messages: list[dict], system: str = "") -> str:
        import copy as _copy
        with self._lock:
            agent = self._get()
            last = next((m["content"] for m in reversed(messages)
                         if m.get("role") == "user"), "")
            prompt = (system + "\n\n" + last) if system else last
            return agent.chat(prompt)

    def json_task(self, prompt: str, system: str = ""):
        import re
        out = self.chat([{"role": "user", "content": prompt}],
                        system=system + "\nRespond ONLY with the JSON, no prose.")
        m = re.search(r"```(?:json)?\s*(.*?)```", out, re.DOTALL)
        text = m.group(1) if m else out
        starts = [i for i in (text.find("["), text.find("{")) if i >= 0]
        if not starts:
            return None
        try:
            return json.loads(text[min(starts):])
        except Exception:
            # try trimming trailing prose
            for end in range(len(text), min(starts), -1):
                try:
                    return json.loads(text[min(starts):end])
                except Exception:
                    continue
        return None


BRIDGE = _BridgeShim()
