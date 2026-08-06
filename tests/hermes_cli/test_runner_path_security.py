import os
from pathlib import Path

import pytest

from hermes_cli.runner import WorkspaceRunner
from hermes_cli.runner_protocol import RunnerCommand
from hermes_cli.runner_spool import RunnerSpool


def make_runner(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    spool = RunnerSpool(tmp_path / "runner.db")
    runner = WorkspaceRunner(spool)
    binding = runner.register_binding(project_id="project-1", root_path=root, label="Repo")
    lease = runner.acquire_lease(
        binding_id=binding.binding_id,
        owner="run-1",
        ttl_seconds=60,
        expected_head=None,
    )
    return root, runner, spool, binding, lease


def command(binding, lease, method: str, params: dict, command_id: str) -> RunnerCommand:
    return RunnerCommand.create(
        attempt_id="attempt-1",
        binding_id=binding.binding_id,
        command_id=command_id,
        fencing_token=lease.fencing_token,
        lease_id=lease.lease_id,
        method=method,
        params=params,
        run_id="run-1",
    )


def test_binding_relative_write_and_read_are_idempotent(tmp_path):
    root, runner, spool, binding, lease = make_runner(tmp_path)

    written = runner.execute(
        command(binding, lease, "fs.write", {"path": "nested/result.txt", "text": "hello"}, "write-1")
    )
    read = runner.execute(
        command(binding, lease, "fs.read", {"path": "nested/result.txt"}, "read-1")
    )
    replay = runner.execute(
        command(binding, lease, "fs.read", {"path": "nested/result.txt"}, "read-1")
    )

    assert written["result"]["bytes_written"] == 5
    assert read["result"] == {"encoding": "utf-8", "text": "hello"}
    assert replay == {**read, "replayed": True}
    assert (root / "nested" / "result.txt").read_text() == "hello"
    assert [event.sequence for event in spool.pending_events("attempt-1")] == list(
        range(1, len(spool.pending_events("attempt-1")) + 1)
    )


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.txt", "nested/../../outside.txt", "C:\\Windows\\win.ini"])
def test_absolute_and_traversal_paths_are_rejected(tmp_path, path):
    _root, runner, _spool, binding, lease = make_runner(tmp_path)

    with pytest.raises(ValueError):
        runner.execute(command(binding, lease, "fs.read", {"path": path}, f"read-{abs(hash(path))}"))


def test_symlink_escape_is_rejected_for_read_and_write(tmp_path):
    root, runner, _spool, binding, lease = make_runner(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    os.symlink(outside, root / "escape")

    with pytest.raises(ValueError, match="symlink|path"):
        runner.execute(
            command(binding, lease, "fs.read", {"path": "escape/secret.txt"}, "read-symlink")
        )

    with pytest.raises(ValueError, match="symlink|path"):
        runner.execute(
            command(binding, lease, "fs.write", {"path": "escape/new.txt", "text": "bad"}, "write-symlink")
        )

    assert not (outside / "new.txt").exists()


def test_stale_lease_is_rejected_before_filesystem_mutation(tmp_path):
    root, runner, _spool, binding, first = make_runner(tmp_path)
    second = runner.acquire_lease(
        binding_id=binding.binding_id,
        owner="run-2",
        ttl_seconds=60,
        expected_head=None,
        now=first.expires_at + 1,
    )

    with pytest.raises(ValueError, match="stale"):
        runner.execute(
            command(binding, first, "fs.write", {"path": "stale.txt", "text": "bad"}, "stale-write")
        )

    assert second.fencing_token > first.fencing_token
    assert not (root / "stale.txt").exists()
