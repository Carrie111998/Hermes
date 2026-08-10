import sys
import threading
import time
from pathlib import Path

import pytest

from hermes_cli.runner import WorkspaceRunner
from hermes_cli.runner_protocol import RunnerCommand
from hermes_cli.runner_spool import RunnerSpool


def setup_runner(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    spool = RunnerSpool(tmp_path / "runner.db")
    runner = WorkspaceRunner(spool, trusted_executables={sys.executable})
    binding = runner.register_binding(project_id="project-1", root_path=root, label="Repo")
    lease = runner.acquire_lease(
        binding_id=binding.binding_id,
        owner="run-1",
        ttl_seconds=120,
        expected_head=None,
    )
    return root, runner, spool, binding, lease


def terminal_command(binding, lease, *, command_id: str, argv: list[str], timeout: float = 10):
    return RunnerCommand.create(
        attempt_id="attempt-terminal",
        binding_id=binding.binding_id,
        command_id=command_id,
        fencing_token=lease.fencing_token,
        lease_id=lease.lease_id,
        method="terminal.run",
        params={"argv": argv, "cwd": ".", "timeout_seconds": timeout},
        run_id="run-terminal",
    )


def test_terminal_runs_without_shell_and_streams_output(tmp_path):
    root, runner, spool, binding, lease = setup_runner(tmp_path)
    result = runner.execute(
        terminal_command(
            binding,
            lease,
            command_id="terminal-1",
            argv=[
                sys.executable,
                "-c",
                "from pathlib import Path; print('hello'); Path('inside.txt').write_text('ok')",
            ],
        )
    )

    assert result["result"]["exit_code"] == 0
    assert "hello" in result["result"]["output"]
    assert (root / "inside.txt").read_text() == "ok"
    events = spool.pending_events("attempt-terminal")
    assert any(event.event_type == "run.output" and "hello" in event.payload["chunk"] for event in events)


def test_terminal_sandbox_denies_writes_outside_binding(tmp_path):
    root, runner, _spool, binding, lease = setup_runner(tmp_path)
    if not runner.terminal_sandbox_available:
        pytest.skip("platform sandbox unavailable")
    outside = tmp_path / "outside.txt"

    result = runner.execute(
        terminal_command(
            binding,
            lease,
            command_id="terminal-outside",
            argv=[
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(outside)!r}).write_text('escape')",
            ],
        )
    )

    assert result["result"]["exit_code"] != 0
    assert not outside.exists()
    assert root.exists()


def test_terminal_rejects_untrusted_shell_executables(tmp_path):
    _root, runner, _spool, binding, lease = setup_runner(tmp_path)

    with pytest.raises(ValueError, match="executable"):
        runner.execute(
            terminal_command(
                binding,
                lease,
                command_id="terminal-shell",
                argv=["/bin/sh", "-c", "touch escaped"],
            )
        )


def test_running_process_can_be_canceled(tmp_path):
    _root, runner, _spool, binding, lease = setup_runner(tmp_path)
    command = terminal_command(
        binding,
        lease,
        command_id="terminal-sleep",
        argv=[sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"],
        timeout=60,
    )
    result: dict = {}

    thread = threading.Thread(target=lambda: result.update(runner.execute(command)))
    thread.start()
    deadline = time.time() + 5
    while command.command_id not in runner.active_process_ids() and time.time() < deadline:
        time.sleep(0.01)

    assert runner.cancel_process(command.command_id) is True
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert result["result"]["canceled"] is True
    assert result["result"]["exit_code"] != 0
