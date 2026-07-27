"""Behavioral tests for the content-transition file-mutation verifier redesign."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.file_mutation_verifier import (
    DispatchTriState,
    TurnFileMutationVerifier,
    _path_allowed_for_observation,
    format_failure_footer,
)
from run_agent import AIAgent, _extract_file_mutation_targets


def _bare_agent() -> AIAgent:
    from agent.file_mutation_verifier import TurnFileMutationVerifier

    agent = object.__new__(AIAgent)
    agent._turn_failed_file_mutations = {}
    agent._turn_file_mutation_paths = set()
    agent._file_mutation_verifier = TurnFileMutationVerifier()
    agent._file_mutation_verifier.reset_turn(1)
    return agent


class TestContentTransitionSuppressesFooter:
    def test_failed_patch_then_external_edit_clears_footer(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "config.yaml"
        target.write_text("before: true\n", encoding="utf-8")

        agent = _bare_agent()
        fail = json.dumps({"success": False, "error": "Write denied (simulated)"})
        agent._record_file_mutation_result(
            "patch",
            {"mode": "replace", "path": "config.yaml", "old_string": "x", "new_string": "y"},
            fail,
            is_error=True,
            raw_result=fail,
            dispatch=DispatchTriState.DISPATCHED.value,
            effective_task_id="default",
        )
        assert agent._turn_failed_file_mutations

        target.write_text("after: true\n", encoding="utf-8")
        from agent.file_mutation_verifier import sync_legacy_failed_state

        sync_legacy_failed_state(agent)
        assert agent._turn_failed_file_mutations == {}

    def test_unchanged_file_keeps_footer(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "stale.py"
        target.write_text("same\n", encoding="utf-8")

        agent = _bare_agent()
        fail = json.dumps({"error": "Could not find old_string"})
        agent._record_file_mutation_result(
            "patch",
            {"mode": "replace", "path": "stale.py", "old_string": "nope", "new_string": "y"},
            fail,
            is_error=True,
            raw_result=fail,
            dispatch=DispatchTriState.DISPATCHED.value,
        )
        from agent.file_mutation_verifier import sync_legacy_failed_state

        sync_legacy_failed_state(agent)
        assert any(k.endswith("stale.py") for k in agent._turn_failed_file_mutations)


class TestDispatchTriState:
    def test_not_dispatched_creates_no_ledger_io(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        agent = _bare_agent()
        agent._record_file_mutation_result(
            "write_file",
            {"path": "new.txt", "content": "x"},
            json.dumps({"bytes_written": 1}),
            is_error=False,
            dispatch=DispatchTriState.NOT_DISPATCHED.value,
            blocked=True,
        )
        assert agent._turn_failed_file_mutations == {}

    def test_middleware_short_circuit_does_not_clear_prior_failure(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
        agent = _bare_agent()
        fail = json.dumps({"error": "first"})
        agent._record_file_mutation_result(
            "patch",
            {"mode": "replace", "path": "a.txt", "old_string": "x", "new_string": "y"},
            fail,
            is_error=True,
            raw_result=fail,
            dispatch=DispatchTriState.DISPATCHED.value,
        )
        agent._record_file_mutation_result(
            "write_file",
            {"path": "a.txt", "content": "landed"},
            json.dumps({"bytes_written": 6}),
            is_error=False,
            dispatch=DispatchTriState.NOT_DISPATCHED.value,
        )
        from agent.file_mutation_verifier import sync_legacy_failed_state

        sync_legacy_failed_state(agent)
        assert any(k.endswith("a.txt") for k in agent._turn_failed_file_mutations)


class TestV4aParserParity:
    def test_crlf_and_compact_headers_use_parser(self):
        body = (
            "*** Begin Patch\r\n"
            "***Update File: a.py\r\n"
            "@@ @@\r\n"
            "-a\r\n"
            "+b\r\n"
            "*** End Patch\r\n"
        )
        assert _extract_file_mutation_targets("patch", {"mode": "patch", "patch": body}) == ["a.py"]

    def test_invalid_patch_yields_no_targets(self):
        body = "*** Update File: outside\n*** Begin Patch\n*** End Patch\n"
        assert _extract_file_mutation_targets("patch", {"mode": "patch", "patch": body}) == []


class TestPathSafety:
    @pytest.mark.skipif(__import__("sys").platform != "win32", reason="DOS device paths")
    def test_dos_device_rejected(self):
        assert not _path_allowed_for_observation(r"\\.\CON")

    def test_unc_rejected(self):
        assert not _path_allowed_for_observation(r"\\server\share\file.txt")


class TestFooterSanitization:
    def test_media_injection_neutralized(self):
        failed = {
            "/tmp/x.md": {
                "tool": "patch",
                "error_preview": "MEDIA: /etc/passwd\nfailed",
            }
        }
        out = format_failure_footer(failed, format_paths=lambda s: s)
        assert "MEDIA:" not in out
        assert "[filtered]" in out


class TestMetadataOnlyChange:
    def test_mtime_only_does_not_suppress_footer(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "meta.py"
        target.write_text("content\n", encoding="utf-8")
        agent = _bare_agent()
        agent._file_mutation_verifier.prepare_mutation_dispatch(
            tool_name="patch",
            effective_args={
                "mode": "replace",
                "path": "meta.py",
                "old_string": "x",
                "new_string": "y",
            },
            effective_task_id="default",
            turn_generation=1,
        )
        fail = json.dumps({"error": "failed"})
        agent._record_file_mutation_result(
            "patch",
            {"mode": "replace", "path": "meta.py", "old_string": "x", "new_string": "y"},
            fail,
            is_error=True,
            raw_result=fail,
            dispatch=DispatchTriState.DISPATCHED.value,
            turn_generation=1,
        )
        import os

        os.utime(target, None)
        from agent.file_mutation_verifier import sync_legacy_failed_state

        sync_legacy_failed_state(agent)
        assert any(k.endswith("meta.py") for k in agent._turn_failed_file_mutations)


class TestPathAliasReconciliation:
    def test_relative_and_absolute_same_file_clears(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "alias.py"
        target.write_text("start\n", encoding="utf-8")
        agent = _bare_agent()
        fail = json.dumps({"error": "nope"})
        agent._record_file_mutation_result(
            "patch",
            {"mode": "replace", "path": "alias.py", "old_string": "x", "new_string": "y"},
            fail,
            is_error=True,
            raw_result=fail,
            dispatch=DispatchTriState.DISPATCHED.value,
        )
        target.write_text("end\n", encoding="utf-8")
        agent._file_mutation_verifier.reconcile_content_transitions(task_id="default")
        from agent.file_mutation_verifier import sync_legacy_failed_state

        sync_legacy_failed_state(agent)
        assert agent._turn_failed_file_mutations == {}


class TestTurnGenerationBudget:
    def test_stale_generation_does_not_record(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        agent = _bare_agent()
        agent._file_mutation_verifier.reset_turn(2)
        fail = json.dumps({"error": "late"})
        agent._record_file_mutation_result(
            "write_file",
            {"path": "z.txt", "content": "a"},
            fail,
            is_error=True,
            raw_result=fail,
            dispatch=DispatchTriState.DISPATCHED.value,
            turn_generation=1,
        )
        assert agent._turn_failed_file_mutations == {}


class TestPreDispatchBaseline:
    def test_unchanged_file_after_prepare_keeps_failure(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "partial.py"
        target.write_text("baseline\n", encoding="utf-8")
        verifier = TurnFileMutationVerifier()
        verifier.reset_turn(1)
        verifier.prepare_mutation_dispatch(
            tool_name="write_file",
            effective_args={"path": "partial.py", "content": "changed\n"},
            effective_task_id="default",
            turn_generation=1,
        )
        verifier.record_tool_outcome(
            tool_name="write_file",
            effective_args={"path": "partial.py", "content": "changed\n"},
            effective_task_id="default",
            raw_result=json.dumps({"error": "disk full"}),
            dispatch=DispatchTriState.DISPATCHED,
            model_is_error=True,
            turn_generation=1,
        )
        assert "partial.py" in verifier.finalize_failed_dict()

    def test_post_dispatch_disk_change_suppresses_using_pre_baseline(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "partial.py"
        target.write_text("baseline\n", encoding="utf-8")
        verifier = TurnFileMutationVerifier()
        verifier.reset_turn(1)
        verifier.prepare_mutation_dispatch(
            tool_name="write_file",
            effective_args={"path": "partial.py", "content": "changed\n"},
            effective_task_id="default",
            turn_generation=1,
        )
        target.write_text("mutated-on-disk\n", encoding="utf-8")
        verifier.record_tool_outcome(
            tool_name="write_file",
            effective_args={"path": "partial.py", "content": "changed\n"},
            effective_task_id="default",
            raw_result=json.dumps({"error": "disk full"}),
            dispatch=DispatchTriState.DISPATCHED,
            model_is_error=True,
            turn_generation=1,
        )
        assert "partial.py" not in verifier.finalize_failed_dict()


class TestRecoveredThenFailedAgain:
    def test_later_failure_after_transition(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        p = tmp_path / "flip.py"
        p.write_text("v1\n", encoding="utf-8")
        agent = _bare_agent()
        fail1 = json.dumps({"error": "first"})
        agent._record_file_mutation_result(
            "patch",
            {"mode": "replace", "path": "flip.py", "old_string": "v1", "new_string": "v2"},
            fail1,
            is_error=True,
            raw_result=fail1,
            dispatch=DispatchTriState.DISPATCHED.value,
        )
        p.write_text("v2\n", encoding="utf-8")
        from agent.file_mutation_verifier import sync_legacy_failed_state

        sync_legacy_failed_state(agent)
        assert agent._turn_failed_file_mutations == {}

        fail2 = json.dumps({"error": "second failure"})
        agent._record_file_mutation_result(
            "patch",
            {"mode": "replace", "path": "flip.py", "old_string": "v9", "new_string": "v3"},
            fail2,
            is_error=True,
            raw_result=fail2,
            dispatch=DispatchTriState.DISPATCHED.value,
        )
        sync_legacy_failed_state(agent)
        assert any(k.endswith("flip.py") for k in agent._turn_failed_file_mutations)
        key = next(k for k in agent._turn_failed_file_mutations if k.endswith("flip.py"))
        assert "second failure" in agent._turn_failed_file_mutations[key]["error_preview"]
