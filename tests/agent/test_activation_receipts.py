"""Behavior contracts for bounded SOUL and skill activation receipts."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.skill_commands import (
    build_preloaded_skills_prompt,
    build_skill_invocation_message,
    scan_skill_commands,
)
from agent.system_prompt import (
    build_system_prompt,
    build_system_prompt_parts,
    invalidate_system_prompt,
)
from cron.scheduler import _build_job_prompt
from hermes_cli.plugins import SHELL_UNSUPPORTED_HOOKS, VALID_HOOKS
from tools.skills_tool import _skill_view_with_bump


def _digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _digest_text(value: str) -> str:
    return _digest_bytes(value.encode("utf-8"))


def _make_prompt_agent(profile_home: Path) -> SimpleNamespace:
    return SimpleNamespace(
        load_soul_identity=True,
        skip_context_files=True,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        _session_db=SimpleNamespace(db_path=profile_home / "state.db"),
        _emit_status=lambda _message: None,
        model="test-model",
        provider="test-provider",
        platform="cli",
        pass_session_id=False,
        session_id="session-receipt",
    )


@contextmanager
def _quiet_prompt_dependencies():
    with (
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
    ):
        yield


def _capture_receipts(monkeypatch):
    from hermes_cli import lifecycle

    receipts = []
    monkeypatch.setattr(
        lifecycle,
        "has_hook",
        lambda name: name == "on_activation_receipt",
    )

    def _capture(name, **kwargs):
        assert name == "on_activation_receipt"
        receipts.append(kwargs["receipt"])

    monkeypatch.setattr(lifecycle, "invoke_hook", _capture)
    return receipts


def _named_profile(tmp_path: Path, monkeypatch, name: str = "bounded") -> Path:
    root = tmp_path / ".hermes"
    profile_home = root / "profiles" / name
    profile_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    return profile_home


def _write_skill(skills_dir: Path, name: str, body: str) -> tuple[Path, bytes]:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True)
    raw = (
        "---\n"
        f"name: {name}\n"
        f"description: {name} receipt fixture\n"
        "---\n\n"
        f"# {name}\n\n"
        f"{body}\n"
    ).encode("utf-8")
    path = skill_dir / "SKILL.md"
    path.write_bytes(raw)
    return path, raw


def test_activation_receipts_are_observer_only_not_shell_hooks():
    from agent import shell_hooks

    assert "on_activation_receipt" in VALID_HOOKS
    assert "on_activation_receipt" in SHELL_UNSUPPORTED_HOOKS
    assert shell_hooks._parse_hooks_block(
        {
            "on_activation_receipt": [
                {"command": "/tmp/must-not-run"},
            ]
        }
    ) == []


def test_soul_receipt_is_emitted_after_real_prompt_construction(
    tmp_path,
    monkeypatch,
):
    profile_home = _named_profile(tmp_path, monkeypatch)
    raw = b"# Bounded soul\n\nUse the exact approved behavior.\n"
    (profile_home / "SOUL.md").write_bytes(raw)
    agent = _make_prompt_agent(profile_home)
    receipts = _capture_receipts(monkeypatch)

    with _quiet_prompt_dependencies():
        prompt = build_system_prompt(agent)

    assert "Use the exact approved behavior." in prompt
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt == {
        "schema_version": 1,
        "receipt_id": receipt["receipt_id"],
        "profile_id": "bounded",
        "session_id": "session-receipt",
        "component_type": "soul",
        "component_name": "SOUL.md",
        "activation_mode": "session_start",
        "raw_digest": _digest_bytes(raw),
        "effective_digest": _digest_text(raw.decode("utf-8").strip()),
    }
    assert receipt["receipt_id"].startswith("activation:")


def test_prompt_part_inspection_does_not_claim_activation(tmp_path, monkeypatch):
    profile_home = _named_profile(tmp_path, monkeypatch)
    (profile_home / "SOUL.md").write_text("# Inspection only\n", encoding="utf-8")
    agent = _make_prompt_agent(profile_home)
    receipts = _capture_receipts(monkeypatch)

    with _quiet_prompt_dependencies():
        parts = build_system_prompt_parts(agent)

    assert "# Inspection only" in parts["stable"]
    assert receipts == []


def test_activation_observer_failure_does_not_change_prompt_behavior(
    tmp_path,
    monkeypatch,
):
    from hermes_cli import lifecycle

    profile_home = _named_profile(tmp_path, monkeypatch)
    (profile_home / "SOUL.md").write_text("# Failure isolation\n", encoding="utf-8")
    agent = _make_prompt_agent(profile_home)
    monkeypatch.setattr(lifecycle, "has_hook", lambda _name: True)

    def _fail(_name, **_kwargs):
        raise RuntimeError("observer failure")

    monkeypatch.setattr(lifecycle, "invoke_hook", _fail)

    with _quiet_prompt_dependencies():
        prompt = build_system_prompt(agent)

    assert "# Failure isolation" in prompt


def test_restored_soul_emits_a_new_receipt_after_cache_invalidation(
    tmp_path,
    monkeypatch,
):
    profile_home = _named_profile(tmp_path, monkeypatch)
    soul_path = profile_home / "SOUL.md"
    original = b"# Original soul\n"
    replacement = b"# Replacement soul\n"
    soul_path.write_bytes(original)
    agent = _make_prompt_agent(profile_home)
    receipts = _capture_receipts(monkeypatch)

    with _quiet_prompt_dependencies():
        agent._cached_system_prompt = build_system_prompt(agent)
        soul_path.write_bytes(replacement)
        invalidate_system_prompt(agent)
        agent._cached_system_prompt = build_system_prompt(agent)
        soul_path.write_bytes(original)
        invalidate_system_prompt(agent)
        agent._cached_system_prompt = build_system_prompt(agent)

    assert [receipt["activation_mode"] for receipt in receipts] == [
        "session_start",
        "cache_rebuild",
        "cache_rebuild",
    ]
    assert len({receipt["receipt_id"] for receipt in receipts}) == 3
    assert receipts[0]["raw_digest"] == receipts[2]["raw_digest"]
    assert receipts[0]["effective_digest"] == receipts[2]["effective_digest"]
    assert receipts[1]["effective_digest"] != receipts[2]["effective_digest"]


def test_slash_and_preload_receipts_bind_each_exact_skill(
    tmp_path,
    monkeypatch,
):
    profile_home = _named_profile(tmp_path, monkeypatch)
    skills_dir = profile_home / "skills"
    first_path, first_raw = _write_skill(
        skills_dir,
        "bounded-first",
        "Follow the first bounded procedure.",
    )
    second_path, second_raw = _write_skill(
        skills_dir,
        "bounded-second",
        "Follow the second bounded procedure.",
    )
    receipts = _capture_receipts(monkeypatch)

    with patch("tools.skills_tool.SKILLS_DIR", skills_dir):
        scan_skill_commands()
        message = build_skill_invocation_message(
            "/bounded-first",
            task_id="session-skills",
        )
        prompt, loaded, missing = build_preloaded_skills_prompt(
            ["bounded-first", "bounded-second"],
            task_id="session-skills",
        )

    assert message and "Follow the first bounded procedure." in message
    assert "Follow the second bounded procedure." in prompt
    assert loaded == ["bounded-first", "bounded-second"]
    assert missing == []
    assert [(receipt["component_name"], receipt["activation_mode"]) for receipt in receipts] == [
        ("bounded-first", "slash"),
        ("bounded-first", "preload"),
        ("bounded-second", "preload"),
    ]
    expected_raw = {
        "bounded-first": _digest_bytes(first_raw),
        "bounded-second": _digest_bytes(second_raw),
    }
    assert receipts[0]["effective_digest"] == _digest_text(
        first_raw.decode("utf-8").strip()
    )
    for receipt in receipts:
        assert receipt["profile_id"] == "bounded"
        assert receipt["session_id"] == "session-skills"
        assert receipt["component_type"] == "skill"
        assert receipt["raw_digest"] == expected_raw[receipt["component_name"]]
        assert receipt["effective_digest"].startswith("sha256:")
        serialized = json.dumps(receipt, sort_keys=True)
        assert str(profile_home) not in serialized
        assert str(first_path) not in serialized
        assert str(second_path) not in serialized
        assert "Follow the first bounded procedure" not in serialized
        assert "Follow the second bounded procedure" not in serialized


def test_cron_skill_receipt_fires_only_when_job_prompt_is_constructed(
    tmp_path,
    monkeypatch,
):
    profile_home = _named_profile(tmp_path, monkeypatch)
    skills_dir = profile_home / "skills"
    _, raw = _write_skill(skills_dir, "bounded-cron", "Run bounded cron work.")
    receipts = _capture_receipts(monkeypatch)

    with patch("tools.skills_tool.SKILLS_DIR", skills_dir):
        prompt = _build_job_prompt(
            {
                "id": "cron-receipt-session",
                "name": "receipt test",
                "prompt": "Perform the scheduled check.",
                "skills": ["bounded-cron"],
            }
        )

    assert "Run bounded cron work." in prompt
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["component_name"] == "bounded-cron"
    assert receipt["activation_mode"] == "cron"
    assert receipt["session_id"] == "cron-receipt-session"
    assert receipt["raw_digest"] == _digest_bytes(raw)


def test_skill_view_receipt_is_closed_and_excludes_content_paths_and_credentials(
    tmp_path,
    monkeypatch,
):
    profile_home = _named_profile(tmp_path, monkeypatch)
    skills_dir = profile_home / "skills"
    _, raw = _write_skill(
        skills_dir,
        "bounded-view",
        "Never disclose credential-canary-7f391f.",
    )
    (profile_home / ".env").write_text(
        "MODEL_API_KEY=raw-credential-canary-4919\n",
        encoding="utf-8",
    )
    receipts = _capture_receipts(monkeypatch)

    with patch("tools.skills_tool.SKILLS_DIR", skills_dir):
        result = json.loads(
            _skill_view_with_bump(
                {"name": "bounded-view"},
                task_id="turn-view",
                session_id="session-view",
            )
        )

    assert result["success"] is True
    assert len(receipts) == 1
    receipt = receipts[0]
    assert set(receipt) == {
        "schema_version",
        "receipt_id",
        "profile_id",
        "session_id",
        "component_type",
        "component_name",
        "activation_mode",
        "raw_digest",
        "effective_digest",
    }
    assert receipt["component_name"] == "bounded-view"
    assert receipt["activation_mode"] == "skill_view"
    assert receipt["raw_digest"] == _digest_bytes(raw)
    assert receipt["effective_digest"] == _digest_text(result["content"])
    serialized = json.dumps(receipt, sort_keys=True)
    assert "credential-canary-7f391f" not in serialized
    assert "raw-credential-canary-4919" not in serialized
    assert str(profile_home) not in serialized
    assert "SKILL.md" not in serialized


def test_failed_or_supporting_file_views_do_not_attest_skill_activation(
    tmp_path,
    monkeypatch,
):
    profile_home = _named_profile(tmp_path, monkeypatch)
    skills_dir = profile_home / "skills"
    skill_path, _ = _write_skill(skills_dir, "bounded-view", "Main instructions.")
    references = skill_path.parent / "references"
    references.mkdir()
    (references / "notes.md").write_text("Supporting notes.", encoding="utf-8")
    receipts = _capture_receipts(monkeypatch)

    with patch("tools.skills_tool.SKILLS_DIR", skills_dir):
        missing = json.loads(
            _skill_view_with_bump(
                {"name": "missing"},
                task_id="turn-view",
                session_id="session-view",
            )
        )
        supporting = json.loads(
            _skill_view_with_bump(
                {"name": "bounded-view", "file_path": "references/notes.md"},
                task_id="turn-view",
                session_id="session-view",
            )
        )

    assert missing["success"] is False
    assert supporting["success"] is True
    assert receipts == []
