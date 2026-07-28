import argparse
import json
import os
from pathlib import Path

import pytest

from hermes_cli import agents_os, agents_os_web
from hermes_cli.agents_os import AgentsOSService, connect, log_event, resolve_paths, utc_now
from hermes_cli.agents_os_web import (
    agents_registry_payload,
    automation_inbox_payload,
    approval_detail_payload,
    artifact_detail_payload,
    artifacts_payload,
    board_meeting_schema_payload,
    create_idea_action,
    draft_board_meeting_action,
    draft_workflow_factory_action,
    governance_action_payload,
    governance_status_payload,
    jarvis_briefing_payload,
    jarvis_model_advisor_payload,
    jarvis_preview_payload,
    jarvis_reply_payload,
    approvals_payload,
    cron_readiness_payload,
    events_payload,
    jarvis_transcribe_payload,
    knowledge_index_payload,
    media_assets_payload,
    mission_control_html,
    redacted_manage_status_payload,
    run_detail_payload,
    runs_payload,
    sessions_visibility_payload,
    skills_visibility_payload,
    task_detail_payload,
    tasks_payload,
    voice_status_payload,
    workflow_factory_schema_payload,
)


def test_automation_bridge_create_dedup_reject_and_approval_gate(agents_home):
    paths = resolve_paths(None)
    service = AgentsOSService(paths)
    payload = {
        "schema_version": "automation-intake.v0",
        "source": "pytest",
        "event_id": "automation-safe-1",
        "goal": "Create a safe local Automation Bridge proof artifact",
        "approval_policy": "safe_local_only",
        "callback": {"type": "local_file"},
    }

    created = service.automation_intake_payload(payload)
    assert created["status"] == "created"
    assert created["exit_code"] == 0
    assert created["approval_required"] is False
    assert Path(created["artifact_path"]).exists()

    deduped = service.automation_intake_payload(payload)
    assert deduped["status"] == "deduped"
    assert deduped["deduped"] is True
    assert deduped["task_id"] == created["task_id"]

    rejected = service.automation_intake_payload({"schema_version": "wrong"})
    assert rejected["status"] == "rejected"
    assert rejected["exit_code"] == 2
    assert rejected["errors"]

    gated = service.automation_intake_payload(
        {**payload, "event_id": "automation-gated-1", "approval_policy": "approval_required"}
    )
    assert gated["status"] == "created"
    assert gated["approval_required"] is True
    with connect(paths) as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (gated["task_id"],)).fetchone()
        approvals = conn.execute("SELECT * FROM approvals WHERE task_id=?", (gated["task_id"],)).fetchall()
    assert task["status"] == "needs_approval"
    assert len(approvals) == 1
    assert approvals[0]["status"] == "pending"

    inbox = automation_inbox_payload(paths)
    assert inbox["count"] == 2
    assert inbox["external_callbacks_executed"] is False
    assert inbox["contract"]["dedup_key"] == "source:event_id"


def test_automation_intake_http_route_maps_rejection_to_400_and_created_to_200(agents_home, monkeypatch):
    service = AgentsOSService(resolve_paths(None))
    captured = {}

    monkeypatch.setattr(agents_os_web, "_read_json_body", lambda handler: {"schema_version": "wrong"})

    def fake_send_json(handler, payload, status=200):
        captured["payload"] = payload
        captured["status"] = status

    monkeypatch.setattr(agents_os_web, "_send_json", fake_send_json)
    handler = object.__new__(agents_os_web.MissionControlHandler)
    handler.path = "/api/automation/intake"
    handler.service = service

    handler.do_POST()

    assert captured["status"] == 400
    assert captured["payload"]["status"] == "rejected"

    monkeypatch.setattr(
        agents_os_web,
        "_read_json_body",
        lambda handler: {
            "schema_version": "automation-intake.v0",
            "source": "pytest-http",
            "event_id": "automation-http-1",
            "goal": "Create a safe local HTTP route proof artifact",
            "callback": {"type": "local_file"},
        },
    )
    captured.clear()

    handler.do_POST()

    assert captured["status"] == 200
    assert captured["payload"]["status"] == "created"


def test_automation_inbox_ui_wires_submit_and_refresh_controls(agents_home):
    html = mission_control_html(AgentsOSService(resolve_paths(None)))

    for marker in (
        'data-tab="automation"',
        'id="automation"',
        'id="automationPayload"',
        'id="submitAutomationPayload"',
        'id="refreshAutomationInbox"',
        'id="automationResult"',
        'id="automationPayloadView"',
        'id="automationList"',
        "$('#submitAutomationPayload').addEventListener('click', submitAutomationPayload);",
        "$('#refreshAutomationInbox').addEventListener('click', refreshAutomationInbox);",
    ):
        assert marker in html


def test_safety_endpoint_alias_returns_dashboard_safety_payload(agents_home, monkeypatch):
    service = AgentsOSService(resolve_paths(None))
    captured = {}

    def fake_send_json(handler, payload, status=200):
        captured["payload"] = payload
        captured["status"] = status

    monkeypatch.setattr(agents_os_web, "_send_json", fake_send_json)
    handler = object.__new__(agents_os_web.MissionControlHandler)
    handler.path = "/api/safety"
    handler.service = service

    handler.do_GET()

    assert captured["status"] == 200
    assert captured["payload"]["gateway_restart"] is False
    assert captured["payload"]["network_side_effects"] is False
    assert "credential_scan" in captured["payload"]
    assert captured["payload"]["credential_scan"]["status"] == "not_run_from_web"
    assert captured["payload"]["doctor"]["checks"]["schema_current"] is True


@pytest.fixture()
def agents_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("AGENTS_OS_HOME", str(home / "agents_os"))
    monkeypatch.setenv("AGENTS_OS_VAULT_ROOT", str(tmp_path / "vault"))
    agents_os.main(["init", "--no-vault"])
    return home


def test_web_module_json_launcher_is_fast_path_without_main_cli_imports(agents_home, capsys):
    code = agents_os_web.main(["--host", "127.0.0.1", "--port", "18791", "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ready"
    assert payload["url"] == "http://127.0.0.1:18791"
    assert payload["local_only"] is True
    assert payload["gateway_restart"] is False


def test_web_cmd_json_launcher_keeps_legacy_argparse_contract(agents_home, capsys):
    args = argparse.Namespace(host="127.0.0.1", port=18791, open=False, json=True)
    code = agents_os_web.web_cmd(args)

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["url"] == "http://127.0.0.1:18791"


def test_root_html_contains_operator_tabs_and_bootstrap_payload(agents_home):
    service = AgentsOSService(resolve_paths(None))
    html = mission_control_html(service)

    assert "Agents OS Mission Control" in html
    for label in [
        "Idea Factory",
        "Agent Registry",
        "Knowledge Galaxy",
        "Board Meeting",
        "Artifact Library",
        "Operator Loop",
        "Media Studio",
        "Manage / Status",
        "Doni",
    ]:
        assert label in html
    assert "/api/idea-factory/draft" in html
    assert "/api/board-meeting/draft" in html
    assert "boardObjective" in html
    assert "boardMeetingResult" in html
    assert "assetSearch" in html
    assert "assetTypeFilter" in html
    assert "demo=task-detail" in html
    assert "demo=approval-detail" in html
    assert "showTaskDetail(tasks.items[0].id)" in html
    assert "showApprovalDetail(approvals.items[0].id)" in html
    assert "vault/reference graph, not runtime memory merge" in html
    assert "Local-only operator cockpit" in html
    assert "Hermes / Doni" in html


def test_idea_factory_schema_and_draft_payloads_are_local_only(agents_home):
    service = AgentsOSService(resolve_paths(None))

    schema = service.idea_factory_schema_payload()
    draft = service.idea_factory_draft_payload({"idea_text": "Pošalji email klijentu"})

    assert schema["local_only"] is True
    assert draft["approval_required"] is True
    assert draft["risk_class"] == "public_gated"
    assert draft["execution_created"] is False


def test_workflow_factory_schema_is_local_only_draft_only(agents_home):
    schema = workflow_factory_schema_payload(resolve_paths(None))

    assert schema["local_only"] is True
    assert schema["execution_created"] is False
    assert schema["external_calls"] is False
    assert schema["mutation_scope"] == "local_artifact_only"
    assert "input_text" in schema["fields"]
    assert "gateway_restart" in schema["approval_gates"]


def test_workflow_factory_draft_classifies_agent_runtime_and_writes_artifact(agents_home):
    service = AgentsOSService(resolve_paths(None))
    result = draft_workflow_factory_action(
        service,
        {"input_text": "Agent OS: dodaj read-only proof panel bez gateway restarta"},
    )

    assert result["status"] == "drafted"
    assert result["local_only"] is True
    assert result["execution_created"] is False
    assert result["task_created"] is False
    assert result["external_calls"] is False
    assert result["capability_bucket"] == "agent_runtime"
    assert result["approval_required"] is True
    assert "gateway_restart" in result["approval_gates"]
    artifact_path = Path(result["artifact_path"])
    assert artifact_path.exists()
    assert "Workflow Factory Draft" in artifact_path.read_text(encoding="utf-8")
    with connect(resolve_paths(None)) as conn:
        artifact = conn.execute("SELECT * FROM artifacts WHERE id=?", (result["artifact_id"],)).fetchone()
    assert artifact["kind"] == "workflow_factory"


def test_workflow_factory_draft_video_stays_safe_local(agents_home):
    service = AgentsOSService(resolve_paths(None))
    result = draft_workflow_factory_action(service, {"source_url": "https://youtu.be/q13OqknCh-c"})

    assert result["input_type"] == "video"
    assert result["capability_bucket"] == "source_ingest"
    assert result["approval_required"] is False
    assert result["execution_created"] is False


def test_board_meeting_schema_is_local_only_and_approval_gated(agents_home):
    schema = board_meeting_schema_payload(resolve_paths(None))

    assert schema["local_only"] is True
    assert schema["execution_created"] is False
    assert schema["external_calls"] is False
    assert "objective" in schema["fields"]
    assert "participants" in schema["fields"]
    assert "credentials" in schema["approval_gates"]
    assert "deploy" in schema["approval_gates"]
    assert "gateway restart" in schema["approval_gates"]


def test_board_meeting_draft_creates_artifact_and_safe_task(agents_home):
    service = AgentsOSService(resolve_paths(None))
    result = draft_board_meeting_action(
        service,
        {"objective": "Planirati Asset Library read-only proof", "participants": ["local-agent", "missing-agent"]},
    )

    assert result["mode"] == "safe_local_task"
    assert result["execution_created"] is False
    assert result["approval_required"] is False
    assert result["approval_id"] is None
    assert result["task_id"].startswith("task-board-")
    assert result["artifact_id"].startswith("artifact-board-")
    assert result["participants"] == ["local-agent"]
    artifact_path = Path(result["artifact_path"])
    assert artifact_path.exists()
    assert "Planirati Asset Library read-only proof" in artifact_path.read_text(encoding="utf-8")
    with connect(resolve_paths(None)) as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (result["task_id"],)).fetchone()
        artifact = conn.execute("SELECT * FROM artifacts WHERE id=?", (result["artifact_id"],)).fetchone()
    assert task["status"] == "pending"
    assert task["route"] == "local:direct"
    assert artifact["kind"] == "board_meeting"


def test_board_meeting_risky_objective_creates_approval_draft_only(agents_home):
    service = AgentsOSService(resolve_paths(None))
    result = draft_board_meeting_action(
        service,
        {"objective": "Pošalji email klijentu i deployaj promjene", "participants": ["local-agent"]},
    )

    assert result["execution_created"] is False
    assert result["approval_required"] is True
    assert result["mode"] == "approval_draft"
    assert result["approval_id"].startswith("approval-board-")
    assert "deploy" in result["approval_gates_triggered"]
    with connect(resolve_paths(None)) as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (result["task_id"],)).fetchone()
        approval = conn.execute("SELECT * FROM approvals WHERE id=?", (result["approval_id"],)).fetchone()
    assert task["status"] == "needs_approval"
    assert approval["status"] == "pending"
    assert approval["risk"] == "approval_gated"


def test_create_idea_action_creates_safe_task_but_gates_public_action(agents_home):
    service = AgentsOSService(resolve_paths(None))

    safe = create_idea_action(service, {"idea_text": "Obradi YouTube video"})
    gated = create_idea_action(service, {"idea_text": "Pošalji email klijentu"})

    assert safe["mode"] == "safe_local_task"
    assert safe["task_id"].startswith("task-")
    assert safe["approval_id"] is None
    assert gated["mode"] == "approval_draft"
    assert gated["task_id"].startswith("task-")
    assert gated["approval_id"].startswith("approval-")
    assert gated["execution_created"] is False
    with connect(resolve_paths(None)) as conn:
        gated_task = conn.execute("SELECT * FROM tasks WHERE id=?", (gated["task_id"],)).fetchone()
        approval = conn.execute("SELECT * FROM approvals WHERE id=?", (gated["approval_id"],)).fetchone()
    assert gated_task["status"] == "needs_approval"
    assert approval["status"] == "pending"


def test_agent_registry_payload_includes_boundaries(agents_home):
    payload = agents_registry_payload(resolve_paths(None))
    ids = {agent["id"] for agent in payload["agents"]}

    assert {"local-agent", "coding-delegate", "separate-profile", "external-reference-runtime"}.issubset(ids)
    local_agent = next(agent for agent in payload["agents"] if agent["id"] == "local-agent")
    assert "shared registry" in local_agent["memory_boundary"]
    assert "gateway restart" in " ".join(local_agent["approval_gates"])
    dumped = json.dumps(payload, ensure_ascii=False)
    assert "Goran" not in dumped
    assert "Marija" not in dumped
    assert "ERO" not in dumped
    assert {"codex", "claude", "openclaw"}.issubset(ids)
    assert "/home/goran" not in dumped
    assert "/mnt/d" not in dumped


def test_knowledge_index_is_non_empty_and_links_video_sources(agents_home, tmp_path, monkeypatch):
    transcript = tmp_path / "q13OqknCh-c_transcript.txt"
    transcript.write_text("Mission Control transcript", encoding="utf-8")
    plan = tmp_path / "2026-06-08-agent-os-full-product-plan.md"
    plan.write_text("# Full product plan", encoding="utf-8")
    monkeypatch.setenv("AGENTS_OS_SOURCE_TRANSCRIPT", str(transcript))
    monkeypatch.setenv("AGENTS_OS_SOURCE_FULL_PLAN", str(plan))

    payload = knowledge_index_payload(resolve_paths(None))

    assert payload["local_only"] is True
    assert payload["runtime_memory_merge"] is False
    assert payload["nodes"]
    assert any(node["id"] == "video:q13OqknCh-c" for node in payload["nodes"])
    assert any(edge["from"] == "video:q13OqknCh-c" for edge in payload["edges"])


def test_artifacts_media_operator_manage_voice_payloads_are_redacted_and_read_only(agents_home, tmp_path):
    paths = resolve_paths(None)
    note = paths.artifacts / "smoke" / "note.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# smoke", encoding="utf-8")
    image = paths.artifacts / "screenshots" / "shot.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    with connect(paths) as conn:
        conn.execute(
            "INSERT INTO artifacts(id,kind,title,path,task_id,workflow,created_at) VALUES(?,?,?,?,?,?,?)",
            ("artifact-test", "smoke_report", "Smoke", str(note), None, "qa-report", utc_now()),
        )
        log_event(conn, "judge_pending", payload={"reason": "not implemented"})
        conn.commit()

    artifacts = artifacts_payload(paths)
    media = media_assets_payload(paths)
    manage = redacted_manage_status_payload(paths)
    voice = voice_status_payload(paths)
    jarvis = jarvis_briefing_payload(paths)
    operator = AgentsOSService(paths).operator_loop_payload()

    assert artifacts["items"]
    assert artifacts["read_only"] is True
    assert artifacts["mutation_enabled"] is False
    assert artifacts["credentials_visible"] is False
    assert artifacts["summary"]
    assert media["assets"]
    assert manage["credentials_visible"] is False
    assert "api_key" not in json.dumps(manage).lower()
    assert voice["computer_control"] == "approval_gated_unexecuted"
    assert jarvis["local_only"] is True
    assert jarvis["execution_created"] is False
    assert jarvis["always_on_microphone"] is False
    assert jarvis["wake_word_enabled"] is False
    assert jarvis["computer_control"] == "approval_gated_unexecuted"
    command_names = {command["name"] for command in jarvis["commands"]}
    assert {"wake", "show", "build", "act"}.issubset(command_names)
    assert {"rundown", "show tasks", "open Google", "SEO keyword ideas", "build website"}.issubset(command_names)
    assert jarvis["briefing"]["artifact_count"] >= 1
    assert "cross_agent_memory_merge" in jarvis["approval_gates"]
    assert operator["judge_status"] in {"pending", "ready"}


def test_task_approval_run_event_and_cron_payloads_are_read_only_and_redacted(agents_home):
    paths = resolve_paths(None)
    with connect(paths) as conn:
        conn.execute(
            "INSERT INTO tasks(id,title,status,workflow,priority,created_at,updated_at,notes,route,approval_required) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("task-visible", "Visible task", "ready", "code-task", 1, utc_now(), utc_now(), "notes", "local:direct", 0),
        )
        conn.execute(
            "INSERT INTO approvals(id,title,status,risk,task_id,payload,created_at) VALUES(?,?,?,?,?,?,?)",
            ("approval-visible", "Visible approval", "pending", "external-action", "task-visible", '{"token":"secret-value"}', utc_now()),
        )
        conn.execute(
            "INSERT INTO runs(id,task_id,workflow,status,input,created_at,completed_at) VALUES(?,?,?,?,?,?,?)",
            ("run-visible", "task-visible", "code-task", "created", "safe input", utc_now(), None),
        )
        log_event(conn, "visible_event", task_id="task-visible", run_id="run-visible", payload={"cookie": "secret"})
        conn.commit()

    tasks = tasks_payload(paths)
    approvals = approvals_payload(paths)
    runs = runs_payload(paths)
    events = events_payload(paths)
    cron = cron_readiness_payload(paths)

    assert tasks["read_only"] is True
    assert any(item["id"] == "task-visible" for item in tasks["items"])
    approval = next(item for item in approvals["items"] if item["id"] == "approval-visible")
    assert approvals["resolution_enabled"] is False
    assert approval["payload_preview"] == "[redacted-sensitive-preview]"
    assert runs["read_only"] is True
    assert any(item["id"] == "run-visible" for item in runs["items"])
    event = next(item for item in events["items"] if item["event_type"] == "visible_event")
    assert event["payload_preview"] == "[redacted-sensitive-preview]"
    assert cron["read_only"] is True
    assert cron["cron_mutation_enabled"] is False



def test_detail_visibility_payloads_are_read_only_and_bounded(agents_home):
    paths = resolve_paths(None)
    artifact_file = paths.artifacts / "detail" / "artifact.md"
    artifact_file.parent.mkdir(parents=True, exist_ok=True)
    artifact_file.write_text("# Evidence\n\nSafe local evidence.", encoding="utf-8")
    with connect(paths) as conn:
        conn.execute(
            "INSERT INTO tasks(id,title,status,workflow,priority,created_at,updated_at,notes,route,approval_required) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("task-detail", "Detail task", "ready", "code-task", 1, utc_now(), utc_now(), "notes", "local:direct", 1),
        )
        conn.execute(
            "INSERT INTO approvals(id,title,status,risk,task_id,payload,created_at) VALUES(?,?,?,?,?,?,?)",
            ("approval-detail", "Detail approval", "pending", "external-action", "task-detail", '{"api_key":"hidden"}', utc_now()),
        )
        conn.execute(
            "INSERT INTO runs(id,task_id,workflow,status,input,created_at,completed_at) VALUES(?,?,?,?,?,?,?)",
            ("run-detail", "task-detail", "code-task", "created", '{"secret":"hidden"}', utc_now(), None),
        )
        conn.execute(
            "INSERT INTO artifacts(id,kind,title,path,task_id,workflow,created_at,run_id) VALUES(?,?,?,?,?,?,?,?)",
            ("artifact-detail", "verification", "Detail artifact", str(artifact_file), "task-detail", "qa-report", utc_now(), "run-detail"),
        )
        log_event(conn, "detail_event", task_id="task-detail", run_id="run-detail", payload={"token": "hidden"})
        conn.commit()

    task = task_detail_payload(paths, "task-detail")
    approval = approval_detail_payload(paths, "approval-detail")
    run = run_detail_payload(paths, "run-detail")
    artifact = artifact_detail_payload(paths, "artifact-detail")
    missing = approval_detail_payload(paths, "missing")

    assert task["read_only"] is True
    assert task["mutation_actions_enabled"] is False
    assert task["approvals"][0]["payload_preview"] == "[redacted-sensitive-preview]"
    assert task["runs"][0]["input_preview"] == "[redacted-sensitive-preview]"
    assert task["events"][0]["payload_preview"] == "[redacted-sensitive-preview]"
    assert approval["resolution_enabled"] is True
    assert approval["approval"]["payload_preview"] == "[redacted-sensitive-preview]"
    assert "execute_without_resolution" in approval["blocked_actions"]
    assert approval["risk_taxonomy"]["deterministic"] is True
    assert run["read_only"] is True
    assert run["run"]["input_preview"] == "[redacted-sensitive-preview]"
    assert artifact["preview_status"] == "ok"
    assert "Evidence" in artifact["preview"]
    assert missing["status"] == "not_found"


def test_skills_and_sessions_visibility_are_metadata_only(agents_home, tmp_path):
    paths = resolve_paths(None)
    skill = paths.home / "skills" / "demo" / "sample" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text('---\nname: sample-skill\ndescription: Sample description\n---\nbody', encoding="utf-8")
    session = paths.home / "sessions" / "demo.json"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text('{"private":"content"}', encoding="utf-8")

    skills = skills_visibility_payload(paths)
    sessions = sessions_visibility_payload(paths)

    assert skills["read_only"] is True
    assert skills["content_visible"] is False
    assert any(item["name"] == "sample-skill" for item in skills["items"])
    assert sessions["metadata_only"] is True
    assert sessions["raw_transcript_visible"] is False
    assert any(item["file"] == "demo.json" for item in sessions["items"])

def test_jarvis_transcribe_writes_local_artifacts_without_execution(agents_home):
    paths = resolve_paths(None)

    payload = jarvis_transcribe_payload(
        paths,
        {
            "audio_base64": "UklGRg==",
            "audio_mime": "audio/webm",
            "transcript_text": "Prikaži zadnje BP24 stanje",
        },
    )

    assert payload["local_only"] is True
    assert payload["execution_created"] is False
    assert payload["status"] == "transcribed"
    assert payload["stt"]["provider"] == "provided_transcript"
    assert payload["advisor"]["provider"] == "deterministic"
    assert payload["transcript"]["text"] == "Prikaži zadnje BP24 stanje"
    assert payload["intent_preview"]["risk_class"] == "safe_local"
    assert payload["intent_preview"]["approval_required"] is False
    assert Path(payload["audio_artifact_path"]).exists()
    assert Path(payload["transcript_artifact_path"]).exists()
    assert "Prikaži zadnje BP24 stanje" in Path(payload["transcript_artifact_path"]).read_text(encoding="utf-8")


def test_jarvis_transcribe_uses_stt_adapter_when_transcript_missing(agents_home):
    paths = resolve_paths(None)

    payload = jarvis_transcribe_payload(
        paths,
        {
            "audio_base64": "UklGRg==",
            "audio_mime": "audio/webm",
            "stt_result": {"text": "Deployaj BP24", "provider": "local-faster-whisper", "confidence": 0.91},
        },
    )

    assert payload["execution_created"] is False
    assert payload["stt"]["provider"] == "local-faster-whisper"
    assert payload["stt"]["confidence"] == 0.91
    assert payload["transcript"]["text"] == "Deployaj BP24"
    assert payload["command_card"]["risk_class"] == "public_gated"
    assert payload["command_card"]["approval_required"] is True


def test_jarvis_transcribe_can_call_local_faster_whisper_adapter(agents_home, monkeypatch):
    paths = resolve_paths(None)
    seen = {}

    def fake_transcribe(audio_path, *, model="base", language="hr"):
        seen["audio_path"] = audio_path
        seen["model"] = model
        seen["language"] = language
        return {"text": "Prikaži zadnje BP24 stanje", "provider": "local-faster-whisper", "confidence": 0.83, "language": "hr"}

    monkeypatch.setattr(agents_os_web, "_transcribe_with_local_faster_whisper", fake_transcribe)

    payload = jarvis_transcribe_payload(
        paths,
        {
            "audio_base64": "UklGRg==",
            "audio_mime": "audio/webm",
            "use_local_stt": True,
            "stt_model": "small",
            "stt_language": "hr",
        },
    )

    assert payload["execution_created"] is False
    assert payload["stt"]["provider"] == "local-faster-whisper"
    assert payload["stt"]["confidence"] == 0.83
    assert payload["transcript"]["text"] == "Prikaži zadnje BP24 stanje"
    assert payload["command_card"]["risk_class"] == "safe_local"
    assert seen["audio_path"] == payload["audio_artifact_path"]
    assert seen["model"] == "small"
    assert seen["language"] == "hr"


def test_jarvis_transcribe_accepts_minimax_cleanup_but_preserves_raw_risk(agents_home):
    paths = resolve_paths(None)

    payload = jarvis_transcribe_payload(
        paths,
        {
            "audio_base64": "UklGRg==",
            "audio_mime": "audio/webm",
            "stt_result": {"text": "Deployaj BP24", "provider": "local-faster-whisper", "confidence": 0.91},
            "model_result": {
                "normalized_transcript": "Prikaži zadnje BP24 stanje",
                "semantic_intent": "status lookup",
                "risk_class": "safe_local",
                "voice_reply_short": "Prikazujem status.",
            },
            "advisor_provider": "minimax",
            "advisor_model": "MiniMax-M3",
        },
    )

    assert payload["execution_created"] is False
    assert payload["transcript"]["text"] == "Deployaj BP24"
    assert payload["transcript"]["cleaned_text"] == "Prikaži zadnje BP24 stanje"
    assert payload["advisor"]["provider"] == "minimax"
    assert payload["advisor"]["model"] == "MiniMax-M3"
    assert payload["advisor"]["risk_disagreement"] is True
    assert payload["command_card"]["risk_class"] == "public_gated"
    assert payload["command_card"]["approval_required"] is True
    assert "Deployaj BP24" in payload["command_card"]["gate_text"]


def test_jarvis_model_advisor_keeps_deterministic_gate_authoritative(agents_home):
    paths = resolve_paths(None)
    deterministic = jarvis_preview_payload(paths, {"transcript_text": "Deployaj BP24"})

    payload = jarvis_model_advisor_payload(
        paths,
        {
            "transcript_text": "Deployaj BP24",
            "deterministic_preview": deterministic,
            "model_result": {"semantic_intent": "deploy production", "risk_class": "safe_local", "voice_reply_short": "Mogu deployati."},
            "provider": "minimax",
            "model": "MiniMax-M3",
        },
    )

    assert payload["execution_created"] is False
    assert payload["provider"] == "minimax"
    assert payload["model"] == "MiniMax-M3"
    assert payload["authoritative_risk_class"] == "public_gated"
    assert payload["model_risk_class"] == "safe_local"
    assert payload["risk_disagreement"] is True
    assert payload["command_card"]["risk_class"] == "public_gated"
    assert payload["command_card"]["approval_required"] is True


def test_jarvis_preview_gates_risky_commands_without_execution(agents_home):
    paths = resolve_paths(None)

    safe = jarvis_preview_payload(paths, {"transcript_text": "Prikaži zadnje BP24 stanje"})
    public = jarvis_preview_payload(paths, {"transcript_text": "Pošalji klijentu email"})
    deploy = jarvis_preview_payload(paths, {"transcript_text": "Deployaj BP24"})
    security = jarvis_preview_payload(paths, {"transcript_text": "Pokreni sigurnosni scan klijentove stranice"})

    assert safe["command_card"]["risk_class"] == "safe_local"
    assert safe["command_card"]["approval_required"] is False
    assert safe["command_card"]["execution_created"] is False
    assert public["command_card"]["risk_class"] == "public_gated"
    assert public["command_card"]["approval_required"] is True
    assert public["command_card"]["execution_created"] is False
    assert deploy["command_card"]["risk_class"] == "public_gated"
    assert deploy["command_card"]["approval_required"] is True
    assert deploy["command_card"]["execution_created"] is False
    assert security["command_card"]["risk_class"] == "security_gated"
    assert security["command_card"]["approval_required"] is True
    assert security["command_card"]["execution_created"] is False


def test_jarvis_reply_stores_tts_audio_artifact_without_execution(agents_home):
    paths = resolve_paths(None)

    payload = jarvis_reply_payload(
        paths,
        {
            "text": "Presuda. Rezultat. Sljedeći korak.",
            "audio_base64": "SUQz",
            "audio_mime": "audio/mpeg",
            "provider": "hermes-tts",
            "voice_reply_short": "Presuda. Rezultat. Sljedeći korak.",
        },
    )

    assert payload["local_only"] is True
    assert payload["execution_created"] is False
    assert payload["status"] == "audio_ready"
    assert payload["tts"]["provider"] == "hermes-tts"
    assert payload["tts"]["fallback"] is False
    assert Path(payload["audio_artifact_path"]).exists()
    assert Path(payload["reply_artifact_path"]).exists()
    assert "Presuda. Rezultat. Sljedeći korak." in Path(payload["reply_artifact_path"]).read_text(encoding="utf-8")


def test_jarvis_reply_falls_back_to_text_only_without_audio(agents_home):
    paths = resolve_paths(None)

    payload = jarvis_reply_payload(paths, {"text": "Ovo treba odobrenje. Ništa ne izvršavam."})

    assert payload["local_only"] is True
    assert payload["execution_created"] is False
    assert payload["status"] == "text_only"
    assert payload["tts"]["provider"] == "text-only-fallback"
    assert payload["tts"]["fallback"] is True
    assert payload["audio_artifact_path"] is None
    assert Path(payload["reply_artifact_path"]).exists()


def test_jarvis_reply_can_prepare_hume_octave_request_draft_without_api_call(agents_home):
    paths = resolve_paths(None)

    payload = jarvis_reply_payload(
        paths,
        {
            "text": "Presuda. Jarvis radi lokalno.",
            "provider": "hume-octave",
            "voice_description": "calm Croatian operator voice, concise and warm",
            "format": "mp3",
        },
    )

    assert payload["execution_created"] is False
    assert payload["status"] == "provider_unconfigured"
    assert payload["tts"]["provider"] == "hume-octave"
    assert payload["tts"]["requires_api_key"] is True
    assert payload["tts"]["api_called"] is False
    assert payload["audio_artifact_path"] is None
    assert payload["hume_octave_request"]["utterances"][0]["text"] == "Presuda. Jarvis radi lokalno."
    assert payload["hume_octave_request"]["utterances"][0]["description"] == "calm Croatian operator voice, concise and warm"
    assert payload["hume_octave_request"]["format"]["type"] == "mp3"


def test_root_html_contains_doni_companion_panel(agents_home):
    service = AgentsOSService(resolve_paths(None))
    html = mission_control_html(service)

    assert "Doni / Osobni asistent" in html
    assert "Voice / Jarvis" not in html
    assert "Jarvis / Oracle Briefing" not in html
    assert "Napiši poruku Doniju" in html
    assert "Record command" in html
    assert "Command Preview" in html
    assert "/api/doni/briefing" in html
    assert "/api/doni/transcribe" in html
    assert "/api/doni/preview" in html
    assert "/api/doni/reply" in html
    assert "Voice Reply" in html
    assert "wake/show/build/act" in html


def test_doni_facade_keeps_legacy_jarvis_routes_compatible(agents_home, monkeypatch):
    service = AgentsOSService(resolve_paths(None))
    captured = {}

    def fake_send_json(handler, payload, status=200):
        captured["payload"] = payload
        captured["status"] = status

    monkeypatch.setattr(agents_os_web, "_send_json", fake_send_json)
    handler = object.__new__(agents_os_web.MissionControlHandler)
    handler.service = service

    handler.path = "/api/doni/briefing"
    handler.do_GET()
    doni = captured["payload"]
    assert captured["status"] == 200

    handler.path = "/api/jarvis/briefing"
    handler.do_GET()
    legacy = captured["payload"]
    assert captured["status"] == 200
    assert doni == legacy

    monkeypatch.setattr(
        agents_os_web,
        "_read_json_body",
        lambda _handler: {"transcript_text": "Prikaži lokalni status"},
    )
    handler.path = "/api/doni/preview"
    handler.do_POST()
    assert captured["status"] == 200
    assert captured["payload"]["execution_created"] is False


def test_doni_companion_proxy_locks_identity_context_and_turn_contract(agents_home, monkeypatch):
    service = AgentsOSService(resolve_paths(None))
    captured = {}
    calls = []

    def fake_send_json(handler, payload, status=200):
        captured["payload"] = payload
        captured["status"] = status

    def fake_companion_request(path, *, method="GET", data=None, timeout_seconds=45.0):
        calls.append({"path": path, "method": method, "data": data, "timeout": timeout_seconds})
        base = {
            "schema_version": "1.0",
            "assistant_identity": "doni",
            "memory_authority": "canonical-doni-runtime",
            "runtime_boot_id": "runtime-test",
        }
        if path == "/v1/companion/sessions":
            return {**base, "session_id": "voice_test"}
        return {
            **base,
            "session_id": "voice_test",
            "turn_id": data["turn_id"],
            "run_id": "run-test",
            "status": "started",
        }

    monkeypatch.setattr(agents_os_web, "_send_json", fake_send_json)
    monkeypatch.setattr(agents_os_web, "doni_companion_json_request", fake_companion_request)
    monkeypatch.setattr(agents_os_web, "_read_json_body", lambda _handler: {})
    handler = object.__new__(agents_os_web.MissionControlHandler)
    handler.service = service

    handler.path = "/api/doni/sessions"
    handler.do_POST()
    assert captured["status"] == 201
    assert captured["payload"]["assistant_identity"] == "doni"
    assert calls[-1]["data"] == {
        "schema_version": "1.0",
        "client": "doni-live-companion",
        "profile_id": "doni",
        "user_id": "goran",
        "locale": "hr-HR",
        "context_policy": "goran_voice_v1",
    }

    monkeypatch.setattr(agents_os_web, "_read_json_body", lambda _handler: {"text": "Koji je sljedeći korak?"})
    handler.path = "/api/doni/sessions/voice_test/turns"
    handler.do_POST()
    assert captured["status"] == 202
    turn = calls[-1]["data"]
    assert calls[-1]["path"] == "/v1/companion/sessions/voice_test/turns"
    assert turn["idempotency_key"] == turn["turn_id"]
    assert turn["locale"] == "hr-HR"
    assert turn["input"] == {"type": "text", "text": "Koji je sljedeći korak?"}
    assert turn["response"]["style"] == "voice_concise"


def test_primary_doni_chat_uses_companion_session_not_action_command(agents_home):
    html = mission_control_html(AgentsOSService(resolve_paths(None)))
    send_block = html.split("async function sendJarvisMessage()", 1)[1].split(
        "async function createJarvisCommand()", 1
    )[0]

    assert "/api/doni/sessions" in send_block
    assert "/api/doni/commands" not in send_block
    assert "assistant_identity" in html
    assert "canonical-doni-runtime" in html


def test_governance_system_map_buttons_are_active_safe_local_controls(agents_home):
    paths = resolve_paths(None)
    service = AgentsOSService(paths)
    html = mission_control_html(service)

    assert "Governance / System Map" in html
    assert "/api/governance/action" in html
    assert "Preview governance artifact" in html
    assert "Draft approval:" in html
    assert "Create local E2E proof task" in html
    assert "read-only showroom" not in html.lower()
    assert "loadAllPromise" in html
    assert "scheduleRefresh" in html
    assert "setInterval(() => loadAll" not in html

    gov = governance_status_payload(paths)
    assert gov["local_only"] is True
    assert gov["execution_created"] is False
    assert len(gov["artifacts"]) >= 1

    preview = governance_action_payload(paths, {"action": "artifact_preview", "artifact_id": gov["artifacts"][0]["id"]})
    assert preview["local_only"] is True
    assert preview["execution_created"] is False
    assert preview["status"] in {"preview_ready", "not_found"}


def test_governance_gated_button_creates_pending_approval_not_execution(agents_home):
    paths = resolve_paths(None)

    result = governance_action_payload(paths, {"action": "gated_approval_draft", "label": "deploy / public publish"})

    assert result["status"] == "approval_drafted"
    assert result["local_only"] is True
    assert result["execution_created"] is False
    assert result["external_calls"] is False
    assert result["approval_id"].startswith("approval-gov-")
    assert result["task_id"].startswith("task-gov-")
    assert Path(result["artifact_path"]).exists()

    approval = approval_detail_payload(paths, result["approval_id"])
    assert approval["status"] == "ok"
    assert approval["approval"]["status"] == "pending"
    assert approval["resolution_enabled"] is True


def test_governance_local_e2e_button_creates_safe_local_task(agents_home):
    paths = resolve_paths(None)

    result = governance_action_payload(paths, {"action": "create_local_e2e_task", "label": "Create local E2E proof task"})

    assert result["status"] == "local_task_created"
    assert result["local_only"] is True
    assert result["execution_created"] is False
    assert result["approval_id"] is None
    task = task_detail_payload(paths, result["task_id"])
    assert task["status"] == "ok"
    assert task["task"]["approval_required"] is False
    assert task["task"]["status"] == "pending"
