from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault(
    "HERMES_HOME",
    str(Path(tempfile.gettempdir()) / "hermes-external-parent-tests"),
)

from hermes_cli.fleet.state import FleetStore
from hermes_state import SessionDB
from tui_gateway.external_parent import ExternalParentSessionDriver


MODEL_ID = "gemini-3.1-pro-high"
MODEL_LABEL = "Gemini 3.1 Pro (High)"
CONVERSATION_ID = "41927196-2e60-44de-9d00-a871f491656c"


def _route() -> dict[str, str]:
    return {
        "model_source": "fleet_auto",
        "fleet_profile_id": "default",
        "fleet_lineage_root_id": "lineage-antigravity",
        "fleet_lane_id": "antigravity",
        "fleet_adapter_kind": "external_cli",
        "fleet_route_purpose": "desktop_parent",
        "fleet_route_identity": "sha256:antigravity-route",
        "model": MODEL_ID,
        "provider": "antigravity-subscription",
        "reasoning_effort": "medium",
        "display_label": "Antigravity · Gemini 3.1 Pro High · external CLI",
    }


def _receipt(*, conversation_id: str, continued: bool) -> str:
    start_id = conversation_id if continued else ""
    lifecycle = (
        f"Print mode: resuming conversation {conversation_id}"
        if continued
        else "\n".join(
            (
                "Starting new conversation (agent=false)",
                f"Created conversation {conversation_id}",
            )
        )
    )
    return "\n".join(
        (
            f'Print mode: starting (promptLength=12, model="{MODEL_LABEL}", '
            f'conversationID="{start_id}")',
            'applyAuthResult: authMethod=consumer, quotaProject=',
            f'Resolving model {MODEL_LABEL}',
            "Propagating selected model override to backend: "
            f'label="{MODEL_LABEL}"',
            lifecycle,
            f"Print mode: conversation={conversation_id}, sending message",
            "URL: https://daily-cloudcode-pa.googleapis.com/"
            "v1internal:streamGenerateContent?alt=sse ResponseID: receipt",
        )
    )


class _Process:
    def __init__(self, stdout: str):
        self.returncode = 0
        self.stdout = stdout
        self.killed = False

    def communicate(self, timeout: int):
        assert timeout == 17
        return self.stdout, ""

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self):
        return self.returncode


def test_driver_starts_then_continues_exact_agy_conversation_by_lineage(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GOOGLE_API_KEY", "must-not-reach-agy")
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-reach-agy")
    calls: list[tuple[list[str], dict]] = []
    outputs = iter(("first answer", "second answer", "third answer"))

    def process_factory(argv, **kwargs):
        argv = list(argv)
        continued = "--conversation" in argv
        if continued:
            assert argv[argv.index("--conversation") + 1] == CONVERSATION_ID
        log_path = Path(argv[argv.index("--log-file") + 1])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            _receipt(
                conversation_id=CONVERSATION_ID,
                continued=continued,
            ),
            encoding="utf-8",
        )
        calls.append((argv, kwargs))
        return _Process(next(outputs))

    store = FleetStore(tmp_path / "fleet" / "state.db")
    db = SessionDB(db_path=tmp_path / "state.db")
    db.ensure_session("stored-antigravity", source="desktop", model=MODEL_ID)
    driver = ExternalParentSessionDriver(
        executable=sys.executable,
        route=_route(),
        cwd=tmp_path,
        session_id="stored-antigravity",
        session_db=db,
        store=store,
        timeout_seconds=17,
        process_factory=process_factory,
    )

    first = driver.run_conversation(
        "turn one",
        conversation_history=[],
        stream_callback=lambda _chunk: None,
    )
    second = driver.run_conversation(
        "turn two",
        conversation_history=first["messages"],
        stream_callback=lambda _chunk: None,
    )

    assert [message["role"] for message in second["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert second["final_response"] == "second answer"
    assert "--conversation" not in calls[0][0]
    assert calls[1][0][calls[1][0].index("--conversation") + 1] == CONVERSATION_ID
    for argv, kwargs in calls:
        assert argv[argv.index("--model") + 1] == MODEL_LABEL
        assert "--print" in argv
        assert "--dangerously-skip-permissions" not in argv
        assert kwargs["cwd"] == tmp_path
        assert kwargs["shell"] is False
        assert not any("API_KEY" in key.upper() for key in kwargs["env"])
        assert "GOOGLE_API_KEY" not in kwargs["env"]
        assert "GEMINI_API_KEY" not in kwargs["env"]

    assert (
        store.read_external_parent_conversation(
            "default",
            "lineage-antigravity",
        )
        == CONVERSATION_ID
    )
    assert [message["role"] for message in db.get_messages_as_conversation("stored-antigravity")] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]

    resumed = ExternalParentSessionDriver(
        executable=sys.executable,
        route=_route(),
        cwd=tmp_path,
        session_id="stored-antigravity",
        session_db=db,
        store=store,
        timeout_seconds=17,
        process_factory=process_factory,
    )
    resumed.run_conversation(
        "turn three",
        conversation_history=second["messages"],
    )
    assert calls[2][0][calls[2][0].index("--conversation") + 1] == CONVERSATION_ID


def test_driver_rejects_mismatched_served_model_without_advancing_history(
    tmp_path,
):
    class WrongModelProcess(_Process):
        pass

    def process_factory(argv, **_kwargs):
        log_path = Path(argv[argv.index("--log-file") + 1])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            _receipt(
                conversation_id=CONVERSATION_ID,
                continued=False,
            ).replace(MODEL_LABEL, "Gemini 3.6 Flash (High)"),
            encoding="utf-8",
        )
        return WrongModelProcess("must not be accepted")

    store = FleetStore(tmp_path / "fleet" / "state.db")
    db = SessionDB(db_path=tmp_path / "state.db")
    db.ensure_session("stored-antigravity", source="desktop", model=MODEL_ID)
    driver = ExternalParentSessionDriver(
        executable=sys.executable,
        route=_route(),
        cwd=tmp_path,
        session_id="stored-antigravity",
        session_db=db,
        store=store,
        timeout_seconds=17,
        process_factory=process_factory,
    )

    with pytest.raises(RuntimeError, match="served-model receipt"):
        driver.run_conversation("turn one", conversation_history=[])

    assert db.get_messages_as_conversation("stored-antigravity") == []
    assert (
        store.read_external_parent_conversation(
            "default",
            "lineage-antigravity",
        )
        is None
    )


def test_driver_kills_timed_out_agy_process(tmp_path):
    process = _Process("")

    def communicate(timeout: int):
        raise subprocess.TimeoutExpired(cmd="agy", timeout=timeout)

    process.communicate = communicate

    driver = ExternalParentSessionDriver(
        executable=sys.executable,
        route=_route(),
        cwd=tmp_path,
        session_id="stored-antigravity",
        session_db=None,
        store=FleetStore(tmp_path / "fleet" / "state.db"),
        timeout_seconds=17,
        process_factory=lambda *_args, **_kwargs: process,
    )

    with pytest.raises(TimeoutError, match="timed out"):
        driver.run_conversation("turn one", conversation_history=[])

    assert process.killed is True


def test_driver_requires_persisted_receipt_before_binding_conversation(
    tmp_path,
    monkeypatch,
):
    def process_factory(argv, **_kwargs):
        log_path = Path(argv[argv.index("--log-file") + 1])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            _receipt(
                conversation_id=CONVERSATION_ID,
                continued=False,
            ),
            encoding="utf-8",
        )
        return _Process("must not be accepted without evidence")

    monkeypatch.setattr(
        "tui_gateway.external_parent._finalize_agy_log",
        lambda *_args, **_kwargs: "not_persisted",
    )
    store = FleetStore(tmp_path / "fleet" / "state.db")
    driver = ExternalParentSessionDriver(
        executable=sys.executable,
        route=_route(),
        cwd=tmp_path,
        session_id="stored-antigravity",
        session_db=None,
        store=store,
        timeout_seconds=17,
        process_factory=process_factory,
    )

    with pytest.raises(RuntimeError, match="receipt evidence"):
        driver.run_conversation("turn one", conversation_history=[])

    assert (
        store.read_external_parent_conversation(
            "default",
            "lineage-antigravity",
        )
        is None
    )
