"""Walk a scenario and emit the canvas event log.

Topology is real. Work is real when ``execute_fn`` calls a model; tests
inject a stub. Human and wait steps persist a park so closing the app
does not lose the run — resume via ``respond`` / ``resolve_event`` /
``tick_timers``.
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from workflow.store import (
    active_run,
    append_event,
    get_document,
    list_runs,
    load_documents,
    load_events,
    load_run,
    new_run_id,
    save_run,
    upsert_document,
)

ExecuteFn = Callable[[str, str, Any, dict], dict]

_execute_fn: ExecuteFn | None = None
_threads: dict[str, threading.Thread] = {}
_thread_lock = threading.Lock()
_run_locks: dict[str, threading.Lock] = {}
_run_locks_guard = threading.Lock()
_timer_threads: dict[str, threading.Thread] = {}


def set_execute_fn(fn: ExecuteFn | None) -> None:
    global _execute_fn
    _execute_fn = fn


def _lock_for(run_id: str) -> threading.Lock:
    with _run_locks_guard:
        lock = _run_locks.get(run_id)
        if lock is None:
            lock = threading.Lock()
            _run_locks[run_id] = lock
        return lock


def _scenario_of(doc_or_scenario: dict) -> dict:
    if "steps" in doc_or_scenario and "edges" in doc_or_scenario:
        return doc_or_scenario
    scenario = doc_or_scenario.get("scenario")
    return scenario if isinstance(scenario, dict) else {"steps": [], "edges": []}


def _steps(scenario: dict) -> list[dict]:
    steps = scenario.get("steps") or []
    return [s for s in steps if isinstance(s, dict) and s.get("id")]


def _edges(scenario: dict) -> list[dict]:
    edges = scenario.get("edges") or []
    return [e for e in edges if isinstance(e, dict) and e.get("source") and e.get("target")]


def _by_id(scenario: dict) -> dict[str, dict]:
    return {s["id"]: s for s in _steps(scenario)}


def _preds(scenario: dict, node_id: str) -> list[str]:
    return [e["source"] for e in _edges(scenario) if e["target"] == node_id]


def _succs(scenario: dict, node_id: str, handle: str | None = None) -> list[str]:
    out = []
    for edge in _edges(scenario):
        if edge["source"] != node_id:
            continue
        if handle is not None and (edge.get("sourceHandle") or "out") != handle:
            continue
        out.append(edge["target"])
    return out


def _kind(step: dict) -> str:
    return str(step.get("kind") or step.get("def", {}).get("kind") or "agent")


def _config(step: dict) -> dict:
    if isinstance(step.get("config"), dict):
        return step["config"]
    return {k: v for k, v in step.items() if k not in {"id", "kind", "def"}}


def _title(step: dict) -> str:
    cfg = _config(step)
    return str(cfg.get("title") or step.get("title") or step["id"])


def parse_wait_seconds(spec: str) -> float | None:
    text = (spec or "").strip().lower()
    every = re.match(r"^every\s+(\d+(?:\.\d+)?)\s*([smhd])", text)
    match = every or re.match(r"^(\d+(?:\.\d+)?)\s*(s|sec|secs|m|min|mins|h|hr|hrs|d|day|days)\b", text)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2)[0]
    return value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _holds(when: dict, inputs: list[dict]) -> bool:
    mode = when.get("mode") or "always"
    if mode == "always":
        return True
    if mode == "all-pass":
        return bool(inputs) and all(i.get("verdict") != "FAIL" for i in inputs)
    if mode == "any-fail":
        return any(i.get("verdict") == "FAIL" for i in inputs)
    if mode == "checks":
        checks = when.get("checks") or []
        hits = []
        for check in checks:
            got = next((i.get("verdict") for i in inputs if i.get("nodeId") == check.get("step")), None)
            is_match = str(got) == str(check.get("value"))
            hits.append(is_match if check.get("op", "is") == "is" else not is_match)
        join = when.get("join") or "all"
        return all(hits) if join == "all" else any(hits)
    if mode == "prose":
        return bool(inputs) and all(i.get("verdict") != "FAIL" for i in inputs)
    return False


def _between(scenario: dict, start: str, end: str) -> list[str]:
    body: set[str] = set()

    def walk(node_id: str, path: list[str]) -> bool:
        if node_id == end:
            body.update([*path, node_id])
            return True
        if node_id in path:
            return False
        return any(walk(target, [*path, node_id]) for target in _succs(scenario, node_id))

    walk(start, [])
    return list(body)


def _emit(state: dict, event_type: str, payload: dict | None = None) -> dict:
    return append_event(state["runId"], event_type, payload)


def _context_for(state: dict, node_id: str) -> str:
    parts = []
    for pred in _preds(state["scenario"], node_id):
        summary = (state.get("summaries") or {}).get(pred)
        output = (state.get("outputs") or {}).get(pred)
        if summary:
            parts.append(f"{pred}: {summary}")
        if output:
            parts.append(f"{pred} output: {output}")
    return "\n".join(parts)


def _fresh_state(workflow_id: str, scenario: dict, payload: Any, source: str, name: str) -> dict:
    return {
        "runId": new_run_id(),
        "workflowId": workflow_id,
        "name": name,
        "scenario": scenario,
        "payload": payload,
        "source": source,
        "status": "running",
        "queue": [],
        "ran": [],
        "satisfied": [],
        "verdicts": {},
        "outputs": {},
        "summaries": {},
        "take": {},
        "loops": 0,
        "park": None,
        "wakeAt": None,
        "waitingEvent": None,
        "pauseRequested": False,
        "seq": 0,
        "startedAt": int(time.time() * 1000),
        "failed": False,
        "tries": {},
        "inFlight": [],
    }


def start_run(
    workflow_id: str,
    *,
    scenario: dict | None = None,
    payload: Any = None,
    source: str = "manual",
    execute_fn: ExecuteFn | None = None,
    background: bool = True,
) -> dict:
    doc = get_document(workflow_id)
    if doc is not None:
        workflow_id = doc["id"]
    if scenario is None:
        if doc is None:
            raise ValueError(f"No workflow called '{workflow_id}'.")
        scenario = _scenario_of(doc)
    else:
        if doc is not None:
            upsert_document({**doc, "scenario": scenario})
        else:
            upsert_document({"id": workflow_id, "name": workflow_id, "scenario": scenario})
            doc = get_document(workflow_id)
    name = (doc or {}).get("name") or workflow_id
    existing = active_run(workflow_id)
    if existing is not None:
        return existing

    state = _fresh_state(workflow_id, scenario, payload, source, name)
    steps = _steps(scenario)
    entries = [s["id"] for s in steps if not _preds(scenario, s["id"])]
    if not entries and steps:
        entries = [steps[0]["id"]]
    state["queue"] = list(entries)
    save_run(state)
    _emit(state, "RunStarted", {"scenario": name})
    if background:
        _spawn(state["runId"], execute_fn)
    else:
        advance(state["runId"], execute_fn=execute_fn)
    return load_run(state["runId"]) or state


def start_from_trigger(workflow_id: str, *, source: str = "cron", payload: Any = None) -> dict:
    return start_run(workflow_id, payload=payload, source=source)


def start_matching(
    *,
    event: str,
    payload: Any = None,
    source: str = "event",
    background: bool = True,
    execute_fn: ExecuteFn | None = None,
) -> list[dict]:
    """Start every workflow whose trigger listens for this event, and resume parks."""
    started = []
    for run_id in resolve_event(event, payload, background=background, execute_fn=execute_fn):
        parked = load_run(run_id)
        if parked is not None:
            started.append(parked)
    needle = (event or "").strip().lower()
    if not needle:
        return started
    for doc in load_documents()["docs"]:
        scenario = _scenario_of(doc)
        for step in _steps(scenario):
            if _kind(step) != "trigger":
                continue
            on = _config(step).get("on") or {}
            if on.get("type") != "event":
                continue
            if str(on.get("spec") or "").strip().lower() != needle:
                continue
            if active_run(doc["id"]) is not None:
                continue
            started.append(
                start_run(
                    doc["id"],
                    payload=payload,
                    source=source,
                    background=background,
                    execute_fn=execute_fn,
                )
            )
            break
    return started


def _spawn(run_id: str, execute_fn: ExecuteFn | None = None) -> None:
    def work() -> None:
        try:
            advance(run_id, execute_fn=execute_fn)
        except Exception as exc:
            state = load_run(run_id)
            if state is None:
                return
            state["status"] = "failed"
            state["failed"] = True
            save_run(state)
            _emit(state, "RunFinished", {"state": "failed", "error": str(exc)})

    thread = threading.Thread(target=work, name=f"workflow-{run_id}", daemon=True)
    with _thread_lock:
        _threads[run_id] = thread
    thread.start()


def advance(run_id: str, *, execute_fn: ExecuteFn | None = None) -> dict:
    with _lock_for(run_id):
        return _advance(run_id, execute_fn)


def _advance(run_id: str, execute_fn: ExecuteFn | None) -> dict:
    state = load_run(run_id)
    if state is None:
        raise ValueError(f"No run '{run_id}'.")
    if state.get("status") in {"succeeded", "failed", "cancelled"}:
        return state
    if state.get("status") == "paused":
        return state
    if state.get("park"):
        return state

    fn = execute_fn or _execute_fn
    scenario = state["scenario"]
    by_id = _by_id(scenario)

    leftover = [node_id for node_id in (state.get("inFlight") or []) if node_id not in state["ran"] and node_id not in state["queue"]]
    if leftover:
        state["queue"] = leftover + state["queue"]
        state["inFlight"] = []
        save_run(state)

    while state["queue"] and state.get("status") == "running":
        if state.get("pauseRequested"):
            state["status"] = "paused"
            save_run(state)
            _emit(state, "RunPaused", {})
            return state

        ran = set(state["ran"])
        satisfied = set(state["satisfied"])
        ready = [
            node_id
            for node_id in state["queue"]
            if all(pred in ran or pred in satisfied or pred not in by_id for pred in _preds(scenario, node_id))
        ]
        if not ready:
            break

        state["queue"] = [node_id for node_id in state["queue"] if node_id not in ready]
        state["inFlight"] = list(ready)
        save_run(state)
        routed: list[str] = []
        halted = False

        def kind_of(node_id: str) -> str:
            step = by_id.get(node_id)
            return _kind(step) if step else ""

        triggers = [n for n in ready if kind_of(n) == "trigger"]
        agents = [n for n in ready if kind_of(n) == "agent"]
        gates = [n for n in ready if kind_of(n) == "gate"]
        waits = [n for n in ready if kind_of(n) == "wait"]
        humans = [n for n in ready if kind_of(n) == "human"]

        for node_id in triggers:
            step = by_id[node_id]
            iteration = int((state["take"].get(node_id) or 0))
            _emit(state, "NodePending", {"nodeId": node_id, "iteration": iteration})
            _run_trigger(state, step, iteration)
            routed.extend(_succs(scenario, node_id))
            state["inFlight"] = [x for x in state["inFlight"] if x != node_id]

        if agents:
            extra, stop = _run_agents(state, [by_id[n] for n in agents], fn)
            routed.extend(extra)
            halted = halted or stop
            state["inFlight"] = [x for x in state["inFlight"] if x not in agents]

        for node_id in gates:
            step = by_id[node_id]
            iteration = int((state["take"].get(node_id) or 0))
            _emit(state, "NodePending", {"nodeId": node_id, "iteration": iteration})
            extra, stop = _run_gate(state, step, iteration, fn)
            routed.extend(extra)
            if stop:
                halted = True
            state["inFlight"] = [x for x in state["inFlight"] if x != node_id]

        parked = False
        for node_id in waits + humans:
            step = by_id[node_id]
            iteration = int((state["take"].get(node_id) or 0))
            _emit(state, "NodePending", {"nodeId": node_id, "iteration": iteration})
            if kind_of(node_id) == "wait":
                if _park_wait(state, step, iteration):
                    parked = True
                else:
                    routed.extend(_succs(scenario, node_id))
            else:
                _park_human(state, step, iteration)
                parked = True
            state["inFlight"] = [x for x in state["inFlight"] if x != node_id]
            if parked:
                for rest in state["inFlight"]:
                    if rest not in state["queue"]:
                        state["queue"].append(rest)
                state["inFlight"] = []
                save_run(state)
                return state

        for nxt in routed:
            if nxt in by_id and nxt not in state["queue"]:
                state["queue"].append(nxt)

        state["inFlight"] = []
        save_run(state)
        if halted:
            state["failed"] = True
            break

    if state.get("park") or state.get("status") == "paused":
        save_run(state)
        return state

    state["status"] = "failed" if state.get("failed") else "succeeded"
    save_run(state)
    _emit(state, "RunFinished", {"state": "failed" if state.get("failed") else "succeeded"})
    return load_run(run_id) or state


def _run_trigger(state: dict, step: dict, iteration: int) -> None:
    node_id = step["id"]
    on = _config(step).get("on") or {"type": "manual", "spec": ""}
    label = f"{on.get('type') or 'manual'}"
    if on.get("spec"):
        label += f" · {on['spec']}"
    _emit(
        state,
        "NodeStarted",
        {"nodeId": node_id, "iteration": iteration, "input": label, "maxIters": 0},
    )
    _emit(state, "NodeFinished", {"nodeId": node_id, "iteration": iteration})
    state["ran"].append(node_id)
    state["take"][node_id] = iteration + 1
    state["verdicts"][node_id] = None


def _arm_matches(arm: dict, inputs: list[dict], state: dict, execute_fn: ExecuteFn | None) -> bool:
    when = arm.get("when") or {}
    if when.get("mode") != "prose":
        return _holds(when, inputs)
    source = str(when.get("source") or "").strip() or "Should this arm be taken? Answer PASS or FAIL."
    context = "\n".join(
        f"{item['nodeId']}: {item.get('verdict') or '—'} · {(state.get('summaries') or {}).get(item['nodeId'], '')}"
        for item in inputs
    )
    if execute_fn is None:
        from workflow.agent import execute_agent_step

        result = execute_agent_step(source, context, state.get("payload"), {"maxIterations": 8})
    else:
        result = execute_fn(source, context, state.get("payload"), {"maxIterations": 8})
    if not result.get("ok", True):
        return False
    verdict = result.get("verdict")
    if verdict:
        return verdict == "PASS"
    text = str(result.get("summary") or "").upper()
    return "PASS" in text or text.startswith("YES")


def _run_gate(state: dict, step: dict, iteration: int, execute_fn: ExecuteFn | None = None) -> tuple[list[str], bool]:
    node_id = step["id"]
    scenario = state["scenario"]
    inputs = [{"nodeId": pred, "verdict": state["verdicts"].get(pred)} for pred in _preds(scenario, node_id)]
    _emit(
        state,
        "NodeStarted",
        {
            "nodeId": node_id,
            "iteration": iteration,
            "input": " · ".join(f"{i['nodeId']} {i['verdict'] or '—'}" for i in inputs) or "no inputs",
            "maxIters": 8,
        },
    )
    arms = _config(step).get("arms") or []
    arm = next((a for a in arms if isinstance(a, dict) and _arm_matches(a, inputs, state, execute_fn)), None)
    route = None
    if arm is not None:
        targets = _succs(scenario, node_id, arm.get("id"))
        route = targets[0] if targets else None
    culprit = next((i for i in inputs if i.get("verdict") == "FAIL"), None)
    decision = "fail" if culprit else "pass"
    title = _title(_by_id(scenario).get(route) or {"id": route or "", "title": "nowhere"})
    _emit(
        state,
        "GateEvaluated",
        {
            "nodeId": node_id,
            "iteration": iteration,
            "inputs": inputs,
            "decision": decision,
            "route": route or "",
            "summary": f"{culprit['nodeId'] + ' FAIL' if culprit else 'group PASS'} → {title}",
        },
    )
    state["ran"].append(node_id)
    state["take"][node_id] = iteration + 1
    state["verdicts"][node_id] = "FAIL" if culprit else "PASS"

    if not route:
        _emit(
            state,
            "NodeFailed",
            {
                "nodeId": node_id,
                "iteration": iteration,
                "error": (
                    f'"{arm.get("label") or arm.get("id")}" isn\'t wired anywhere'
                    if arm
                    else "no arm matched, so the work has nowhere to go"
                ),
            },
        )
        state["failed"] = True
        return [], True

    if route in state["ran"]:
        cap = int(_config(step).get("maxLoops") or 5)
        if state["loops"] >= cap:
            _emit(state, "NodeFailed", {"nodeId": node_id, "iteration": iteration, "error": f"gave up after {cap} takes"})
            state["failed"] = True
            return [], True
        state["loops"] += 1
        _emit(
            state,
            "LoopAdvanced",
            {
                "loopId": node_id,
                "iteration": state["loops"],
                "to": route,
                "feedback": f"{culprit['nodeId']} feedback" if culprit else "another take",
            },
        )
        body = _between(scenario, route, node_id)
        rerun = []
        for item in body:
            if item in state["ran"]:
                state["ran"] = [x for x in state["ran"] if x != item]
            if item not in {route, node_id} and state["verdicts"].get(item) == "PASS":
                if item not in state["satisfied"]:
                    state["satisfied"].append(item)
                _emit(
                    state,
                    "NodeSkipped",
                    {
                        "nodeId": item,
                        "iteration": state["loops"],
                        "reason": f"satisfied · PASS on take {state['take'].get(item) or 1}",
                    },
                )
            else:
                rerun.append(item)
        return [route], False

    return [route], False


def _park_human(state: dict, step: dict, iteration: int) -> None:
    cfg = _config(step)
    who = str(cfg.get("assignee") or "you").strip() or "you"
    prompt = str(cfg.get("goal") or "").strip() or f"{_title(step)} — approve?"
    payload = {
        "nodeId": step["id"],
        "iteration": iteration,
        "prompt": prompt,
        "who": who,
        "onFail": cfg.get("onFail") or "halt",
    }
    _emit(state, "HumanWaiting", payload)
    state["status"] = "waiting_human"
    state["park"] = {"kind": "human", **payload}


def _park_wait(state: dict, step: dict, iteration: int) -> bool:
    until = _config(step).get("until") or {"type": "timer", "spec": ""}
    kind = str(until.get("type") or "timer")
    spec = str(until.get("spec") or "").strip()
    label = spec or kind
    _emit(
        state,
        "WaitStarted",
        {"nodeId": step["id"], "iteration": iteration, "until": f"{kind} · {label}", "label": label},
    )
    # Only a timer is a clock. Event and poll both wait on the bus — poll
    # used to sleep one interval and lie that it had asked the world.
    if kind != "timer":
        state["status"] = "waiting_world"
        state["waitingEvent"] = spec or kind
        state["park"] = {
            "kind": "wait",
            "nodeId": step["id"],
            "iteration": iteration,
            "until": kind,
            "by": "event received",
        }
        return True
    seconds = parse_wait_seconds(spec)
    if seconds is None:
        seconds = 0
    if seconds <= 0:
        _finish_wait(state, step["id"], iteration, "elapsed")
        return False
    wake = time.time() + seconds
    state["status"] = "waiting_world"
    state["wakeAt"] = wake
    state["park"] = {"kind": "wait", "nodeId": step["id"], "iteration": iteration, "until": kind, "by": "elapsed"}
    _arm_timer(state["runId"], seconds)
    return True


def _finish_wait(state: dict, node_id: str, iteration: int, by: str) -> None:
    _emit(state, "WaitResolved", {"nodeId": node_id, "iteration": iteration, "by": by})
    state["ran"].append(node_id)
    state["take"][node_id] = iteration + 1
    state["verdicts"][node_id] = None
    state["park"] = None
    state["wakeAt"] = None
    state["waitingEvent"] = None
    state["status"] = "running"


def _compute_agent(state: dict, step: dict, iteration: int, execute_fn: ExecuteFn | None) -> dict:
    node_id = step["id"]
    cfg = _config(step)
    goal = str(cfg.get("goal") or "").strip() or _title(step)
    context = "" if cfg.get("blind") else _context_for(state, node_id)
    traces: list[tuple[str, str]] = []

    def on_tool(name: str, arg: str = "") -> None:
        traces.append((name, arg))

    if execute_fn is None:
        from workflow.agent import execute_agent_step

        result = execute_agent_step(goal, context, state.get("payload"), cfg, on_tool=on_tool)
    else:
        result = execute_fn(goal, context, state.get("payload"), cfg)
    if traces:
        result = {**result, "_traces": traces}
    return result


def _apply_agent(state: dict, step: dict, iteration: int, result: dict) -> str:
    node_id = step["id"]
    cfg = _config(step)
    for name, arg in result.get("_traces") or []:
        _emit(
            state,
            "AgentTraceEvent",
            {"nodeId": node_id, "iteration": iteration, "tool": {"name": name, "arg": arg}},
        )
    if not result.get("ok", True):
        error = str(result.get("error") or "step failed")
        _emit(state, "NodeFailed", {"nodeId": node_id, "iteration": iteration, "error": error})
        retries = int(state.setdefault("tries", {}).get(node_id) or 0)
        allowed = int(cfg.get("maxRetries") or 0)
        if retries < allowed:
            state["tries"][node_id] = retries + 1
            return "retry"
        state["ran"].append(node_id)
        state["take"][node_id] = iteration + 1
        state["verdicts"][node_id] = "FAIL"
        on_fail = cfg.get("onFail") or "halt"
        if on_fail == "route":
            return "route"
        state["failed"] = True
        return "halt"

    summary = str(result.get("summary") or "done")
    verdict = result.get("verdict")
    output = result.get("output") if isinstance(result.get("output"), dict) else {"text": summary}
    _emit(state, "AgentTraceSummary", {"nodeId": node_id, "iteration": iteration, "summary": summary, "verdict": verdict})
    _emit(state, "TaskOutput", {"nodeId": node_id, "iteration": iteration, "output": output})
    _emit(state, "NodeFinished", {"nodeId": node_id, "iteration": iteration})
    state["ran"].append(node_id)
    state["take"][node_id] = iteration + 1
    state["verdicts"][node_id] = verdict
    state["summaries"][node_id] = summary
    state["outputs"][node_id] = output
    return "ok"


def _run_agents(state: dict, steps: list[dict], execute_fn: ExecuteFn | None) -> tuple[list[str], bool]:
    routed: list[str] = []
    halted = False
    prepared = []
    for step in steps:
        iteration = int((state["take"].get(step["id"]) or 0))
        cfg = _config(step)
        goal = str(cfg.get("goal") or "").strip() or _title(step)
        _emit(state, "NodePending", {"nodeId": step["id"], "iteration": iteration})
        _emit(
            state,
            "NodeStarted",
            {
                "nodeId": step["id"],
                "iteration": iteration,
                "input": goal[:80],
                "maxIters": int(cfg.get("maxIterations") or 20),
                "loop": iteration > 0,
            },
        )
        prepared.append((step, iteration))

    results: dict[str, dict] = {}
    if len(prepared) == 1:
        step, iteration = prepared[0]
        results[step["id"]] = _compute_agent(state, step, iteration, execute_fn)
    else:
        with ThreadPoolExecutor(max_workers=min(8, len(prepared))) as pool:
            futs = {
                pool.submit(_compute_agent, state, step, iteration, execute_fn): step["id"]
                for step, iteration in prepared
            }
            for fut in as_completed(futs):
                results[futs[fut]] = fut.result()

    for step, iteration in prepared:
        stop = _apply_agent(state, step, iteration, results[step["id"]])
        if stop == "halt":
            halted = True
        elif stop == "retry":
            state["queue"].append(step["id"])
        else:
            routed.extend(_succs(state["scenario"], step["id"]))
    return routed, halted


def respond(run_id: str, node_id: str, decision: str, *, by: str | None = None, execute_fn: ExecuteFn | None = None) -> dict:
    with _lock_for(run_id):
        state = load_run(run_id)
        if state is None:
            raise ValueError(f"No run '{run_id}'.")
        park = state.get("park") or {}
        if park.get("kind") != "human" or park.get("nodeId") != node_id:
            raise ValueError("This run is not waiting on that person.")
        choice = "approved" if decision == "approved" else "denied"
        who = by or park.get("who") or "you"
        iteration = int(park.get("iteration") or 0)
        _emit(state, "HumanResponded", {"nodeId": node_id, "iteration": iteration, "decision": choice, "by": who})
        state["park"] = None
        if choice == "approved":
            state["ran"].append(node_id)
            state["take"][node_id] = iteration + 1
            state["verdicts"][node_id] = "PASS"
            state["status"] = "running"
            for nxt in _succs(state["scenario"], node_id):
                if nxt not in state["queue"]:
                    state["queue"].append(nxt)
            save_run(state)
        else:
            on_fail = park.get("onFail") or "halt"
            if on_fail == "retry":
                state["status"] = "running"
                if node_id not in state["queue"]:
                    state["queue"].append(node_id)
                save_run(state)
            else:
                state["failed"] = True
                state["ran"].append(node_id)
                state["take"][node_id] = iteration + 1
                state["verdicts"][node_id] = "FAIL"
                state["status"] = "failed"
                save_run(state)
                _emit(state, "RunFinished", {"state": "failed"})
                return load_run(run_id) or state
    return advance(run_id, execute_fn=execute_fn)


def resolve_event(
    event: str,
    payload: Any = None,
    *,
    background: bool = True,
    execute_fn: ExecuteFn | None = None,
) -> list[str]:
    """Resume every run parked on this event name. Payload is recorded on the wait."""
    needle = (event or "").strip().lower()
    resumed: list[str] = []
    if not needle:
        return resumed
    for state in list_runs():
        if state.get("status") != "waiting_world":
            continue
        waiting = str(state.get("waitingEvent") or "").strip().lower()
        if waiting != needle:
            continue
        park = state.get("park") or {}
        if park.get("kind") != "wait":
            continue
        with _lock_for(state["runId"]):
            live = load_run(state["runId"])
            if live is None or live.get("status") != "waiting_world":
                continue
            if payload is not None:
                live["payload"] = payload
            node_id = park["nodeId"]
            _finish_wait(live, node_id, int(park.get("iteration") or 0), "event received")
            for nxt in _succs(live["scenario"], node_id):
                if nxt not in live["queue"]:
                    live["queue"].append(nxt)
            save_run(live)
        if background:
            _spawn(state["runId"], execute_fn)
        else:
            advance(state["runId"], execute_fn=execute_fn)
        resumed.append(state["runId"])
    return resumed


def _arm_timer(run_id: str, seconds: float) -> None:
    def fire() -> None:
        time.sleep(max(0.0, seconds))
        tick_timers(run_id=run_id)

    thread = threading.Thread(target=fire, name=f"workflow-timer-{run_id}", daemon=True)
    with _thread_lock:
        _timer_threads[run_id] = thread
    thread.start()


def tick_timers(run_id: str | None = None) -> list[str]:
    """Resume timer parks whose wake time has passed. Called on a timer thread and at boot."""
    now = time.time()
    resumed: list[str] = []
    runs = [load_run(run_id)] if run_id else list_runs()
    for state in runs:
        if not state or state.get("status") != "waiting_world":
            continue
        wake = state.get("wakeAt")
        if wake is None or float(wake) > now:
            continue
        park = state.get("park") or {}
        if park.get("kind") != "wait":
            continue
        with _lock_for(state["runId"]):
            live = load_run(state["runId"])
            if live is None or live.get("status") != "waiting_world":
                continue
            node_id = park["nodeId"]
            _finish_wait(live, node_id, int(park.get("iteration") or 0), park.get("by") or "elapsed")
            for nxt in _succs(live["scenario"], node_id):
                if nxt not in live["queue"]:
                    live["queue"].append(nxt)
            save_run(live)
        _spawn(state["runId"])
        resumed.append(state["runId"])
    return resumed


def request_pause(run_id: str) -> dict:
    state = load_run(run_id)
    if state is None:
        raise ValueError(f"No run '{run_id}'.")
    if state.get("park"):
        return state
    state["pauseRequested"] = True
    save_run(state)
    return state


def resume_run(run_id: str, *, execute_fn: ExecuteFn | None = None) -> dict:
    state = load_run(run_id)
    if state is None:
        raise ValueError(f"No run '{run_id}'.")
    if state.get("status") != "paused":
        return state
    state["pauseRequested"] = False
    state["status"] = "running"
    save_run(state)
    if execute_fn is not None:
        return advance(run_id, execute_fn=execute_fn)
    _spawn(run_id)
    return load_run(run_id) or state


def cancel_run(run_id: str) -> dict:
    state = load_run(run_id)
    if state is None:
        raise ValueError(f"No run '{run_id}'.")
    state["status"] = "cancelled"
    state["park"] = None
    state["queue"] = []
    save_run(state)
    _emit(state, "RunFinished", {"state": "failed"})
    return load_run(run_id) or state


def rearm_parked() -> None:
    """On gateway start: resume running work, wake due timers, leave humans parked."""
    tick_timers()
    for state in list_runs():
        status = state.get("status")
        if status == "running":
            _spawn(state["runId"])
            continue
        if status != "waiting_world":
            continue
        wake = state.get("wakeAt")
        if wake is None:
            continue
        remaining = float(wake) - time.time()
        if remaining > 0:
            _arm_timer(state["runId"], remaining)


def snapshot(run_id: str, after: int = -1) -> dict:
    state = load_run(run_id)
    if state is None:
        raise ValueError(f"No run '{run_id}'.")
    return {"run": state, "events": load_events(run_id, after)}


def snapshot_active(workflow_id: str) -> dict | None:
    state = active_run(workflow_id)
    if state is None:
        return None
    return {"run": state, "events": load_events(state["runId"])}
