"""The runner walks the authored graph and parks for people and the world."""

import threading

from workflow.runner import (
    advance,
    parse_wait_seconds,
    resolve_event,
    respond,
    start_matching,
    start_run,
)
from workflow.store import load_events, save_documents, save_run


def _agent(_goal, context, payload, _config):
    return {
        "ok": True,
        "summary": f"did it · {payload}",
        "verdict": "PASS",
        "output": {"seen": payload, "context": context},
    }


def _scenario(*steps, edges=None):
    return {"steps": list(steps), "edges": list(edges or [])}


def _put(monkeypatch, tmp_path, doc):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    save_documents([doc], doc["id"])


def test_parse_wait_seconds():
    assert parse_wait_seconds("30s") == 30
    assert parse_wait_seconds("2h") == 7200
    assert parse_wait_seconds("every 5m") == 300
    assert parse_wait_seconds("github.pull_request.merged") is None


def test_agent_receives_trigger_payload(tmp_path, monkeypatch):
    _put(
        monkeypatch,
        tmp_path,
        {
            "id": "hooked",
            "name": "hooked",
            "scenario": _scenario(
                {"id": "start", "kind": "trigger", "config": {"title": "Hook", "on": {"type": "webhook", "spec": ""}}},
                {"id": "work", "kind": "agent", "config": {"title": "Work", "goal": "handle it"}},
                edges=[{"id": "start->work", "source": "start", "target": "work"}],
            ),
        },
    )
    state = start_run("hooked", payload={"pr": 12}, source="webhook", execute_fn=_agent, background=False)
    assert state["status"] == "succeeded"
    assert state["outputs"]["work"]["seen"] == {"pr": 12}
    types = [e["type"] for e in load_events(state["runId"])]
    assert types[0] == "RunStarted"
    assert "NodeFinished" in types
    assert types[-1] == "RunFinished"


def test_human_parks_and_survives_reload(tmp_path, monkeypatch):
    _put(
        monkeypatch,
        tmp_path,
        {
            "id": "approve",
            "name": "approve",
            "scenario": _scenario(
                {"id": "ask", "kind": "human", "config": {"title": "Ship?", "goal": "Ship it?"}},
                {"id": "ship", "kind": "agent", "config": {"title": "Ship", "goal": "open the PR"}},
                edges=[{"id": "ask->ship", "source": "ask", "target": "ship"}],
            ),
        },
    )
    parked = start_run("approve", execute_fn=_agent, background=False)
    assert parked["status"] == "waiting_human"
    assert parked["park"]["nodeId"] == "ask"
    done = respond(parked["runId"], "ask", "approved", execute_fn=_agent)
    assert done["status"] == "succeeded"
    assert "ship" in done["ran"]


def test_poll_wait_parks_on_the_bus_not_a_timer(tmp_path, monkeypatch):
    _put(
        monkeypatch,
        tmp_path,
        {
            "id": "poll",
            "name": "poll",
            "scenario": _scenario(
                {
                    "id": "hold",
                    "kind": "wait",
                    "config": {"title": "Green", "until": {"type": "poll", "spec": "deploy.green"}},
                }
            ),
        },
    )
    parked = start_run("poll", execute_fn=_agent, background=False)
    assert parked["status"] == "waiting_world"
    assert parked["waitingEvent"] == "deploy.green"


def test_wait_event_resumes_on_the_same_bus(tmp_path, monkeypatch):
    _put(
        monkeypatch,
        tmp_path,
        {
            "id": "listen",
            "name": "listen",
            "scenario": _scenario(
                {
                    "id": "hold",
                    "kind": "wait",
                    "config": {"title": "PR", "until": {"type": "event", "spec": "github.pull_request.merged"}},
                },
                {"id": "work", "kind": "agent", "config": {"title": "Work", "goal": "continue"}},
                edges=[{"id": "hold->work", "source": "hold", "target": "work"}],
            ),
        },
    )
    parked = start_run("listen", execute_fn=_agent, background=False)
    assert parked["status"] == "waiting_world"
    resolve_event("github.pull_request.merged", {"merged": True}, background=False, execute_fn=_agent)
    from workflow.store import load_run

    done = load_run(parked["runId"])
    assert done["status"] == "succeeded"
    assert "work" in done["ran"]


def test_event_trigger_starts_matching_workflow(tmp_path, monkeypatch):
    _put(
        monkeypatch,
        tmp_path,
        {
            "id": "on-merge",
            "name": "on-merge",
            "scenario": _scenario(
                {
                    "id": "go",
                    "kind": "trigger",
                    "config": {"title": "Merged", "on": {"type": "event", "spec": "github.pull_request.merged"}},
                },
                {"id": "work", "kind": "agent", "config": {"title": "Work", "goal": "ship"}},
                edges=[{"id": "go->work", "source": "go", "target": "work"}],
            ),
        },
    )
    started = start_matching(
        event="github.pull_request.merged",
        payload={"n": 1},
        background=False,
        execute_fn=_agent,
    )
    assert len(started) == 1
    assert started[0]["status"] == "succeeded"
    assert started[0]["outputs"]["work"]["seen"] == {"n": 1}


def test_gate_routes_on_verdicts(tmp_path, monkeypatch):
    def judge(goal, _context, _payload, _config):
        return {"ok": True, "summary": "FAIL", "verdict": "FAIL", "output": {"verdict": "FAIL"}}

    _put(
        monkeypatch,
        tmp_path,
        {
            "id": "gated",
            "name": "gated",
            "scenario": _scenario(
                {"id": "check", "kind": "agent", "config": {"title": "Check", "goal": "review"}},
                {
                    "id": "gate",
                    "kind": "gate",
                    "config": {
                        "title": "Gate",
                        "arms": [
                            {"id": "pass", "when": {"mode": "all-pass"}},
                            {"id": "loop", "when": {"mode": "any-fail"}},
                        ],
                    },
                },
                {"id": "ship", "kind": "agent", "config": {"title": "Ship", "goal": "open"}},
                {"id": "fix", "kind": "agent", "config": {"title": "Fix", "goal": "fix"}},
                edges=[
                    {"id": "check->gate", "source": "check", "target": "gate"},
                    {"id": "gate->ship", "source": "gate", "target": "ship", "sourceHandle": "pass"},
                    {"id": "gate->fix", "source": "gate", "target": "fix", "sourceHandle": "loop"},
                ],
            ),
        },
    )
    state = start_run("gated", execute_fn=judge, background=False)
    assert state["status"] == "succeeded"
    assert "fix" in state["ran"]
    assert "ship" not in state["ran"]


def test_ready_agents_run_together(tmp_path, monkeypatch):
    first = threading.Event()
    second = threading.Event()

    def pair(goal, _context, _payload, _config):
        if goal == "left":
            first.set()
            assert second.wait(2)
        else:
            assert first.wait(2)
            second.set()
        return {"ok": True, "summary": goal, "verdict": "PASS", "output": {"goal": goal}}

    _put(
        monkeypatch,
        tmp_path,
        {
            "id": "fan",
            "name": "fan",
            "scenario": _scenario(
                {"id": "left", "kind": "agent", "config": {"title": "Left", "goal": "left"}},
                {"id": "right", "kind": "agent", "config": {"title": "Right", "goal": "right"}},
            ),
        },
    )
    state = start_run("fan", execute_fn=pair, background=False)
    assert state["status"] == "succeeded"
    assert set(state["ran"]) == {"left", "right"}


def test_prose_gate_takes_the_pass_arm(tmp_path, monkeypatch):
    def fn(goal, _context, _payload, _config):
        if "ship it" in goal.lower():
            return {"ok": True, "summary": "PASS", "verdict": "PASS", "output": {}}
        return {"ok": True, "summary": "drafted", "verdict": "PASS", "output": {}}

    _put(
        monkeypatch,
        tmp_path,
        {
            "id": "prose",
            "name": "prose",
            "scenario": _scenario(
                {"id": "draft", "kind": "agent", "config": {"title": "Draft", "goal": "write"}},
                {
                    "id": "gate",
                    "kind": "gate",
                    "config": {
                        "title": "Ship?",
                        "arms": [
                            {"id": "yes", "when": {"mode": "prose", "source": "Should we ship it?"}},
                            {"id": "no", "when": {"mode": "any-fail"}},
                        ],
                    },
                },
                {"id": "open", "kind": "agent", "config": {"title": "Open", "goal": "pr"}},
                {"id": "hold", "kind": "agent", "config": {"title": "Hold", "goal": "wait"}},
                edges=[
                    {"id": "draft->gate", "source": "draft", "target": "gate"},
                    {"id": "gate->open", "source": "gate", "target": "open", "sourceHandle": "yes"},
                    {"id": "gate->hold", "source": "gate", "target": "hold", "sourceHandle": "no"},
                ],
            ),
        },
    )
    state = start_run("prose", execute_fn=fn, background=False)
    assert state["status"] == "succeeded"
    assert "open" in state["ran"]
    assert "hold" not in state["ran"]


def test_inflight_is_restored_on_advance(tmp_path, monkeypatch):
    _put(
        monkeypatch,
        tmp_path,
        {
            "id": "crash",
            "name": "crash",
            "scenario": _scenario(
                {"id": "work", "kind": "agent", "config": {"title": "Work", "goal": "ship"}},
            ),
        },
    )
    save_run(
        {
            "runId": "crash-1",
            "workflowId": "crash",
            "name": "crash",
            "scenario": _scenario(
                {"id": "work", "kind": "agent", "config": {"title": "Work", "goal": "ship"}},
            ),
            "payload": {"n": 7},
            "source": "manual",
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
            "startedAt": 1,
            "failed": False,
            "tries": {},
            "inFlight": ["work"],
        }
    )
    state = advance("crash-1", execute_fn=_agent)
    assert state["status"] == "succeeded"
    assert "work" in state["ran"]
    assert state["outputs"]["work"]["seen"] == {"n": 7}
