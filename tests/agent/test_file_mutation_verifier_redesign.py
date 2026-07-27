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
    sync_legacy_failed_state,
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

    def test_dispatched_failure_without_targets_fails_closed(self):
        from agent.file_mutation_verifier import _UNKNOWN_MUTATION_TARGET

        verifier = TurnFileMutationVerifier(use_subprocess_fingerprint=False)
        verifier.reset_turn(1)
        body = "*** Update File: outside\n*** Begin Patch\n*** End Patch\n"
        fail = json.dumps({"error": "Could not apply patch"})
        verifier.record_tool_outcome(
            tool_name="patch",
            effective_args={"mode": "patch", "patch": body},
            effective_task_id="default",
            raw_result=fail,
            dispatch=DispatchTriState.DISPATCHED,
            model_is_error=True,
            turn_generation=1,
        )
        failed = verifier.finalize_failed_dict()
        assert _UNKNOWN_MUTATION_TARGET in failed
        assert "Could not apply patch" in failed[_UNKNOWN_MUTATION_TARGET]["error_preview"]


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
        verifier = TurnFileMutationVerifier(use_subprocess_fingerprint=False)
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
        verifier = TurnFileMutationVerifier(use_subprocess_fingerprint=False)
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


def _slow_fingerprint_worker(path_str, out_q):
    import time

    time.sleep(5)
    out_q.put((None, 0))


class TestFailsafeObservation:
    def test_subprocess_timeout_leaves_no_live_workers(self, tmp_path, monkeypatch):
        import agent.file_mutation_verifier as fmv

        target = tmp_path / "slow.bin"
        target.write_bytes(b"abc")

        monkeypatch.setattr(fmv, "_mp_fingerprint_worker", _slow_fingerprint_worker)
        monkeypatch.setattr(fmv, "OBSERVATION_TIMEOUT_S", 0.15)

        verifier = TurnFileMutationVerifier(use_subprocess_fingerprint=True)
        verifier.reset_turn(1)
        monkeypatch.chdir(tmp_path)
        fp = verifier._capture_baseline("slow.bin", "default", turn_generation=1)
        assert fp is None
        assert verifier.active_worker_pids == set()

    def test_symlink_is_not_observable(self, tmp_path):
        import os
        import time

        from agent.file_mutation_verifier import _stable_local_fingerprint_inprocess

        if os.name == "nt":
            pytest.skip("symlink lstat guard is POSIX-focused in this test")
        real = tmp_path / "real.txt"
        real.write_text("data\n", encoding="utf-8")
        link = tmp_path / "link.txt"
        link.symlink_to(real)
        fp, _ = _stable_local_fingerprint_inprocess(
            link, deadline=time.monotonic() + 5.0,
        )
        assert fp is None

    def test_torn_read_rejected(self, tmp_path, monkeypatch):
        import time

        from agent.file_mutation_verifier import _stable_local_fingerprint_inprocess

        target = tmp_path / "torn.txt"
        target.write_text("stable\n", encoding="utf-8")
        real_stat = target.stat()

        def _unstable_stat(path, *, follow_symlinks=True):
            if not follow_symlinks:
                raise OSError("simulated torn read")
            return real_stat

        monkeypatch.setattr(__import__("os"), "stat", _unstable_stat)
        fp, _ = _stable_local_fingerprint_inprocess(
            target, deadline=time.monotonic() + 5.0,
        )
        assert fp is None

    def test_posix_dialect_preserves_backslash_identity(self):
        verifier = TurnFileMutationVerifier(
            resolve_backend=lambda _tid: ("local", "host", "posix"),
        )
        a = verifier._identity_for_path("dir\\file.py", "default")
        b = verifier._identity_for_path("dir/file.py", "default")
        assert a is not None and b is not None
        assert a.path != b.path

    def test_ssh_backend_never_local_clears(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "remote.py"
        target.write_text("a\n", encoding="utf-8")
        verifier = TurnFileMutationVerifier(
            resolve_backend=lambda _tid: ("ssh", "SSHSession", "posix"),
            use_subprocess_fingerprint=False,
        )
        verifier.reset_turn(1)
        verifier.record_tool_outcome(
            tool_name="write_file",
            effective_args={"path": "remote.py", "content": "b\n"},
            effective_task_id="default",
            raw_result=json.dumps({"error": "fail"}),
            dispatch=DispatchTriState.DISPATCHED,
            model_is_error=True,
            turn_generation=1,
        )
        target.write_text("b\n", encoding="utf-8")
        verifier.reconcile_content_transitions(task_id="default")
        assert "remote.py" in verifier.finalize_failed_dict()


class TestRegistryDispatchAuthority:
    def test_handle_function_call_middleware_short_circuit_not_dispatched(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.chdir(tmp_path)

        def _short_circuit(name, args, execute, **kwargs):
            return json.dumps({"success": True, "bytes_written": 12})

        monkeypatch.setattr(
            "hermes_cli.middleware.run_tool_execution_middleware",
            _short_circuit,
        )
        from model_tools import (
            begin_tool_registry_dispatch_tracking,
            end_tool_registry_dispatch_tracking,
            handle_function_call,
            tool_registry_was_dispatched,
        )

        token = begin_tool_registry_dispatch_tracking()
        try:
            handle_function_call(
                "write_file",
                {"path": "never.txt", "content": "x"},
                task_id="default",
                skip_pre_tool_call_hook=True,
            )
            dispatched = tool_registry_was_dispatched()
        finally:
            end_tool_registry_dispatch_tracking(token)
        assert not dispatched

        agent = _bare_agent()
        agent._record_file_mutation_result(
            "patch",
            {"mode": "replace", "path": "a.txt", "old_string": "x", "new_string": "y"},
            json.dumps({"error": "first"}),
            is_error=True,
            raw_result=json.dumps({"error": "first"}),
            dispatch=DispatchTriState.DISPATCHED.value,
        )
        agent._record_file_mutation_result(
            "write_file",
            {"path": "never.txt", "content": "x"},
            json.dumps({"success": True, "bytes_written": 12}),
            is_error=False,
            dispatch=DispatchTriState.NOT_DISPATCHED.value,
        )
        sync_legacy_failed_state(agent)
        assert any(k.endswith("a.txt") for k in agent._turn_failed_file_mutations)
