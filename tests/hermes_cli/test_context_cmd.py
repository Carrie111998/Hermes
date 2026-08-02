from argparse import Namespace
import os
import subprocess
import sys
from types import SimpleNamespace


def test_context_verify_links_allowlisted_check_to_active_candidate(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from agent import learning_ledger
    from hermes_cli.context_cmd import cmd_context

    learning_ledger.create_candidate(
        {
            "candidate_id": "verify-candidate",
            "subsystem": "skills",
            "action": "edit",
            "status": "pending",
            "payload_fingerprint": "sha256:" + "a" * 64,
            "dedup_key": "verify-candidate",
            "proposal": {"name": "demo"},
        }
    )
    learning_ledger.transition_candidate(
        "verify-candidate",
        from_status="pending",
        to_status="active",
        event="candidate_applied",
    )
    monkeypatch.setattr(
        "hermes_cli.context_cmd.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="1 passed\n", stderr=""),
    )

    result = cmd_context(
        Namespace(
            context_action="verify",
            candidate_id="verify-candidate",
            verify_argv=["--", "pytest", "tests/agent/test_learning_ledger.py"],
        )
    )

    assert result == 0
    assert "verification passed" in capsys.readouterr().out
    candidate = learning_ledger.get_candidate("verify-candidate")
    assert candidate is not None
    assert candidate["status"] == "validated"


def test_context_verify_rejects_non_allowlisted_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from agent import learning_ledger
    from hermes_cli.context_cmd import cmd_context

    learning_ledger.create_candidate(
        {
            "candidate_id": "unsafe-candidate",
            "subsystem": "memory",
            "action": "add",
            "status": "active",
            "payload_fingerprint": "sha256:" + "b" * 64,
            "dedup_key": "unsafe-candidate",
            "proposal": {"target": "memory"},
        }
    )

    result = cmd_context(
        Namespace(
            context_action="verify",
            candidate_id="unsafe-candidate",
            verify_argv=["--", "sh", "-c", "echo nope"],
        )
    )

    assert result == 2
    assert "not an allowlisted" in capsys.readouterr().out


def test_context_verify_fails_closed_when_outcome_is_not_durable(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from agent import learning_ledger
    from hermes_cli.context_cmd import cmd_context

    learning_ledger.create_candidate(
        {
            "candidate_id": "durability-candidate",
            "subsystem": "skills",
            "action": "edit",
            "status": "pending",
            "payload_fingerprint": "sha256:" + "d" * 64,
            "dedup_key": "durability-candidate",
            "proposal": {"name": "demo"},
        }
    )
    learning_ledger.transition_candidate(
        "durability-candidate",
        from_status="pending",
        to_status="active",
        event="candidate_applied",
    )
    monkeypatch.setattr(
        "hermes_cli.context_cmd.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="1 passed\n", stderr=""),
    )
    monkeypatch.setattr(
        learning_ledger,
        "record_outcome",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("disk unavailable")),
    )

    result = cmd_context(
        Namespace(
            context_action="verify",
            candidate_id="durability-candidate",
            verify_argv=["--", "pytest", "tests/agent/test_learning_ledger.py"],
        )
    )

    output = capsys.readouterr().out
    assert result == 2
    assert "could not be persisted" in output
    assert "verification passed" not in output
    candidate = learning_ledger.get_candidate("durability-candidate")
    assert candidate is not None and candidate["status"] == "active"


def test_context_verify_cli_preserves_top_level_command_dispatch(tmp_path):
    env = dict(os.environ)
    env["HERMES_HOME"] = str(tmp_path / ".hermes")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "context",
            "verify",
            "missing-candidate",
            "--",
            "pytest",
            "tests/hermes_cli/test_context_cmd.py",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert completed.returncode == 2
    assert "known active learning candidate" in completed.stdout
    assert "unhashable type" not in completed.stderr
