from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "safe_change_runner.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("safe_change_runner", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def read_report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_verified_transaction_keeps_change_and_writes_audit_report(tmp_path):
    runner = load_runner()
    workdir = tmp_path / "repo"
    target = workdir / "config.txt"
    apply_script = workdir / "apply.py"
    verify_script = workdir / "verify.py"
    report_path = workdir / "reports" / "safe-change.json"
    target.parent.mkdir(parents=True)
    target.write_text("scale=90\n", encoding="utf-8")
    write(apply_script, """
        from pathlib import Path
        Path('config.txt').write_text('scale=70\\n', encoding='utf-8')
    """)
    write(verify_script, """
        from pathlib import Path
        assert Path('config.txt').read_text(encoding='utf-8') == 'scale=70\\n'
    """)

    rc = runner.main([
        "--workdir", str(workdir),
        "--name", "autoscaling-threshold",
        "--snapshot", "config.txt",
        "--apply-json", json.dumps([sys.executable, "apply.py"]),
        "--verify-json", json.dumps([sys.executable, "verify.py"]),
        "--report", str(report_path),
        "--retry-delays", "0",
    ])

    assert rc == 0
    assert target.read_text(encoding="utf-8") == "scale=70\n"
    report = read_report(report_path)
    assert report["status"] == "verified"
    assert report["rollback_performed"] is False
    assert report["name"] == "autoscaling-threshold"
    assert report["snapshot"]["items"][0]["before_sha256"]
    assert report["commands"]["apply"][0]["attempts"][0]["exit_code"] == 0
    assert report["commands"]["verify"][0]["attempts"][0]["exit_code"] == 0


def test_verify_failure_rolls_back_to_exact_previous_file_contents(tmp_path):
    runner = load_runner()
    workdir = tmp_path / "repo"
    target = workdir / "skill.md"
    apply_script = workdir / "apply_bad.py"
    verify_script = workdir / "verify_bad.py"
    report_path = workdir / "safe-change-failed.json"
    target.parent.mkdir(parents=True)
    target.write_text("stable procedure\n", encoding="utf-8")
    write(apply_script, """
        from pathlib import Path
        Path('skill.md').write_text('broken procedure\\n', encoding='utf-8')
    """)
    write(verify_script, """
        raise SystemExit('verification rejected the change')
    """)

    rc = runner.main([
        "--workdir", str(workdir),
        "--name", "skill-rollback",
        "--snapshot", "skill.md",
        "--apply-json", json.dumps([sys.executable, "apply_bad.py"]),
        "--verify-json", json.dumps([sys.executable, "verify_bad.py"]),
        "--report", str(report_path),
        "--retry-delays", "0",
    ])

    assert rc == 1
    assert target.read_text(encoding="utf-8") == "stable procedure\n"
    report = read_report(report_path)
    assert report["status"] == "rolled_back"
    assert report["rollback_performed"] is True
    assert report["failure_phase"] == "verify"
    assert "verification rejected" in report["commands"]["verify"][0]["attempts"][0]["stderr"]


def test_apply_uses_bounded_backoff_retries_before_succeeding(tmp_path):
    runner = load_runner()
    workdir = tmp_path / "repo"
    target = workdir / "cron.txt"
    counter = workdir / "counter.txt"
    apply_script = workdir / "flaky_apply.py"
    verify_script = workdir / "verify.py"
    report_path = workdir / "backoff-report.json"
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    write(apply_script, """
        from pathlib import Path
        counter = Path('counter.txt')
        attempts = int(counter.read_text(encoding='utf-8')) if counter.exists() else 0
        counter.write_text(str(attempts + 1), encoding='utf-8')
        if attempts == 0:
            raise SystemExit('transient cloud capacity failure')
        Path('cron.txt').write_text('new\\n', encoding='utf-8')
    """)
    write(verify_script, """
        from pathlib import Path
        assert Path('cron.txt').read_text(encoding='utf-8') == 'new\\n'
    """)

    rc = runner.main([
        "--workdir", str(workdir),
        "--name", "backoff-success",
        "--snapshot", "cron.txt",
        "--apply-json", json.dumps([sys.executable, "flaky_apply.py"]),
        "--verify-json", json.dumps([sys.executable, "verify.py"]),
        "--report", str(report_path),
        "--retry-delays", "0,0",
    ])

    assert rc == 0
    assert target.read_text(encoding="utf-8") == "new\n"
    assert counter.read_text(encoding="utf-8") == "2"
    report = read_report(report_path)
    attempts = report["commands"]["apply"][0]["attempts"]
    assert [attempt["exit_code"] for attempt in attempts] == [1, 0]
    assert attempts[0]["delay_before_next_seconds"] == 0.0


def test_directory_snapshot_is_restored_when_apply_command_fails(tmp_path):
    runner = load_runner()
    workdir = tmp_path / "repo"
    config_dir = workdir / "config"
    nested = config_dir / "service.yaml"
    apply_script = workdir / "apply_fail.py"
    verify_script = workdir / "verify.py"
    report_path = workdir / "dir-rollback.json"
    config_dir.mkdir(parents=True)
    nested.write_text("replicas: 1\n", encoding="utf-8")
    write(apply_script, """
        from pathlib import Path
        Path('config/service.yaml').write_text('replicas: 99\\n', encoding='utf-8')
        raise SystemExit('apply rejected')
    """)
    write(verify_script, """
        raise SystemExit('verify should not run')
    """)

    rc = runner.main([
        "--workdir", str(workdir),
        "--name", "dir-rollback",
        "--snapshot", "config",
        "--apply-json", json.dumps([sys.executable, "apply_fail.py"]),
        "--verify-json", json.dumps([sys.executable, "verify.py"]),
        "--report", str(report_path),
        "--retry-delays", "0",
    ])

    assert rc == 1
    assert nested.read_text(encoding="utf-8") == "replicas: 1\n"
    report = read_report(report_path)
    assert report["failure_phase"] == "apply"
    assert report["commands"]["verify"] == []
    assert report["snapshot"]["items"][0]["kind"] == "dir"


def test_missing_snapshot_path_is_removed_again_during_rollback(tmp_path):
    runner = load_runner()
    workdir = tmp_path / "repo"
    target = workdir / "new-file.txt"
    apply_script = workdir / "create_then_fail_verify.py"
    verify_script = workdir / "verify_fail.py"
    report_path = workdir / "missing-rollback.json"
    workdir.mkdir()
    write(apply_script, """
        from pathlib import Path
        Path('new-file.txt').write_text('temporary\\n', encoding='utf-8')
    """)
    write(verify_script, """
        raise SystemExit('new file not accepted')
    """)

    rc = runner.main([
        "--workdir", str(workdir),
        "--name", "remove-new-file",
        "--snapshot", "new-file.txt",
        "--apply-json", json.dumps([sys.executable, "create_then_fail_verify.py"]),
        "--verify-json", json.dumps([sys.executable, "verify_fail.py"]),
        "--report", str(report_path),
        "--retry-delays", "0",
    ])

    assert rc == 1
    assert not target.exists()
    report = read_report(report_path)
    assert report["snapshot"]["items"][0]["existed"] is False


def test_command_timeout_is_reported_and_rolled_back(tmp_path):
    runner = load_runner()
    workdir = tmp_path / "repo"
    target = workdir / "timeout.txt"
    slow_script = workdir / "slow.py"
    verify_script = workdir / "verify.py"
    report_path = workdir / "timeout-report.json"
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    write(slow_script, """
        import time
        from pathlib import Path
        Path('timeout.txt').write_text('new\\n', encoding='utf-8')
        time.sleep(2)
    """)
    write(verify_script, """
        raise SystemExit('verify should not run')
    """)

    rc = runner.main([
        "--workdir", str(workdir),
        "--name", "timeout",
        "--snapshot", "timeout.txt",
        "--apply-json", json.dumps([sys.executable, "slow.py"]),
        "--verify-json", json.dumps([sys.executable, "verify.py"]),
        "--report", str(report_path),
        "--retry-delays", "0",
        "--timeout", "0.1",
    ])

    assert rc == 1
    assert target.read_text(encoding="utf-8") == "old\n"
    report = read_report(report_path)
    assert report["commands"]["apply"][0]["attempts"][0]["exit_code"] == 124
    assert "timed out" in report["commands"]["apply"][0]["attempts"][0]["stderr"]


def test_timeout_kills_delayed_child_before_rollback_can_be_undone(tmp_path):
    runner = load_runner()
    workdir = tmp_path / "repo"
    target = workdir / "timeout-child.txt"
    slow_script = workdir / "spawn_child.py"
    verify_script = workdir / "verify.py"
    report_path = workdir / "timeout-child-report.json"
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    child_code = "import time; from pathlib import Path; time.sleep(0.4); Path('timeout-child.txt').write_text('late\\n', encoding='utf-8')"
    write(slow_script, f"""
        import subprocess
        import sys
        import time
        from pathlib import Path
        Path('timeout-child.txt').write_text('new\\n', encoding='utf-8')
        subprocess.Popen([sys.executable, '-c', {child_code!r}])
        time.sleep(2)
    """)
    write(verify_script, """
        raise SystemExit('verify should not run')
    """)

    rc = runner.main([
        "--workdir", str(workdir),
        "--name", "timeout-child",
        "--snapshot", "timeout-child.txt",
        "--apply-json", json.dumps([sys.executable, "spawn_child.py"]),
        "--verify-json", json.dumps([sys.executable, "verify.py"]),
        "--report", str(report_path),
        "--retry-delays", "0",
        "--timeout", "0.1",
    ])
    time.sleep(0.7)

    assert rc == 1
    assert target.read_text(encoding="utf-8") == "old\n"
    report = read_report(report_path)
    assert "process tree terminated" in report["commands"]["apply"][0]["attempts"][0]["stderr"]


def test_preflight_validation_helpers_reject_bad_inputs(tmp_path):
    runner = load_runner()
    workdir = tmp_path / "repo"
    workdir.mkdir()

    assert runner.parse_retry_delays(" , 0") == [0.0]

    for raw in ["not-json", json.dumps([]), json.dumps("echo unused")]:
        try:
            runner.parse_json_argv(raw)
        except runner.SafeChangeError:
            pass
        else:  # pragma: no cover - assertion guard
            raise AssertionError(f"accepted invalid argv: {raw}")

    for raw in ["abc", "-1"]:
        try:
            runner.parse_retry_delays(raw)
        except runner.SafeChangeError:
            pass
        else:  # pragma: no cover - assertion guard
            raise AssertionError(f"accepted invalid retry delay: {raw}")

    try:
        runner.validate_snapshot_paths(workdir, [])
    except runner.SafeChangeError as exc:
        assert "at least one --snapshot" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("accepted missing snapshot paths")

    try:
        runner.resolve_snapshot_path(workdir, ".")
    except runner.SafeChangeError as exc:
        assert "workdir root" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("accepted workdir root snapshot")


def test_preflight_reports_missing_apply_and_verify_after_path_policy(tmp_path):
    runner = load_runner()
    workdir = tmp_path / "repo"
    target = workdir / "file.txt"
    report_path = workdir / "report.json"
    workdir.mkdir()
    target.write_text("stable\n", encoding="utf-8")

    rc_missing_apply = runner.main([
        "--workdir", str(workdir),
        "--name", "missing-apply",
        "--snapshot", "file.txt",
        "--verify-json", json.dumps([sys.executable, "-c", "print('verify')"]),
        "--report", str(report_path),
    ])
    assert rc_missing_apply == 2
    assert "--apply-json" in read_report(report_path)["error"]

    rc_missing_verify = runner.main([
        "--workdir", str(workdir),
        "--name", "missing-verify",
        "--snapshot", "file.txt",
        "--apply-json", json.dumps([sys.executable, "-c", "print('apply')"]),
        "--report", str(report_path),
    ])
    assert rc_missing_verify == 2
    assert "--verify-json" in read_report(report_path)["error"]


def test_fifo_snapshot_and_corrupt_snapshot_kind_fail_closed(tmp_path):
    runner = load_runner()
    if not hasattr(__import__("os"), "mkfifo"):
        return
    import os

    workdir = tmp_path / "repo"
    workdir.mkdir()
    fifo = workdir / "pipe"
    os.mkfifo(fifo)

    try:
        runner.create_snapshot(workdir, "fifo", ["pipe"])
    except runner.SafeChangeError as exc:
        assert "neither file nor directory" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("accepted unsupported FIFO snapshot")

    snapshot = runner.SnapshotReport(
        root=workdir.as_posix(),
        items=[runner.SnapshotItem("target", True, "unsupported", "missing-backup", None)],
    )
    try:
        runner.rollback(workdir, snapshot)
    except runner.SafeChangeError as exc:
        assert "unsupported snapshot kind" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("accepted corrupt snapshot kind")


def test_backoff_with_nonzero_delay_is_recorded(tmp_path):
    runner = load_runner()
    workdir = tmp_path / "repo"
    script = workdir / "always_fail.py"
    workdir.mkdir()
    write(script, """
        raise SystemExit('still failing')
    """)

    report = runner.run_command([sys.executable, "always_fail.py"], workdir, timeout=5, retry_delays=[0.01, 0])

    assert [attempt.exit_code for attempt in report.attempts] == [1, 1]
    assert report.attempts[0].delay_before_next_seconds == 0.01


def test_process_tree_terminator_noops_when_process_already_finished():
    runner = load_runner()

    class DoneProcess:
        def poll(self):
            return 0

    runner._terminate_process_tree(DoneProcess())


def test_process_tree_terminator_escalates_to_sigkill(monkeypatch):
    runner = load_runner()
    calls = []

    class HangingProcess:
        pid = 4321

        def poll(self):
            return None

        def wait(self, timeout):
            raise TimeoutError("still alive")

        def kill(self):  # pragma: no cover - should not be used on POSIX path
            raise AssertionError("unexpected direct kill")

    def fake_killpg(pid, sig):
        calls.append((pid, sig))

    monkeypatch.setattr(runner.os, "killpg", fake_killpg, raising=False)

    runner._terminate_process_tree(HangingProcess())

    assert calls == [(4321, runner.signal.SIGTERM), (4321, runner.signal.SIGKILL)]


def test_process_tree_terminator_handles_process_lookup_race(monkeypatch):
    runner = load_runner()
    calls = []

    class VanishedProcess:
        pid = 111

        def poll(self):
            return None

        def wait(self, timeout):
            raise AssertionError("wait should not run when initial killpg raises")

        def kill(self):  # pragma: no cover - process already gone
            raise AssertionError("unexpected direct kill")

    def fake_killpg(pid, sig):
        calls.append((pid, sig))
        raise ProcessLookupError(pid)

    monkeypatch.setattr(runner.os, "killpg", fake_killpg, raising=False)

    runner._terminate_process_tree(VanishedProcess())

    assert calls == [(111, runner.signal.SIGTERM), (111, runner.signal.SIGKILL)]


def test_process_tree_terminator_falls_back_to_direct_kill(monkeypatch):
    runner = load_runner()
    killed = []

    class StubbornProcess:
        pid = 222

        def poll(self):
            return None

        def wait(self, timeout):
            raise TimeoutError("still alive")

        def kill(self):
            killed.append(True)

    def fake_killpg(pid, sig):
        raise RuntimeError("permission denied")

    monkeypatch.setattr(runner.os, "killpg", fake_killpg, raising=False)

    runner._terminate_process_tree(StubbornProcess())

    assert killed == [True]


def test_timeout_report_includes_partial_stdout_and_stderr(monkeypatch, tmp_path):
    runner = load_runner()
    workdir = tmp_path / "repo"
    workdir.mkdir()

    class FakeProcess:
        pid = 333
        returncode = None

        def __init__(self, *args, **kwargs):
            pass

        def communicate(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired(["fake"], timeout, output="partial-out", stderr="partial-err")
            return "tail-out", "tail-err"

        def poll(self):
            return None

        def wait(self, timeout):
            return 0

    monkeypatch.setattr(runner.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: None, raising=False)

    report = runner.run_command([sys.executable, "fake.py"], workdir, timeout=0.1, retry_delays=[0])

    attempt = report.attempts[0]
    assert attempt.exit_code == 124
    assert attempt.stdout == "partial-outtail-out"
    assert "partial-errtail-err" in attempt.stderr


def test_preflight_rejects_snapshot_paths_outside_workdir(tmp_path):
    runner = load_runner()
    workdir = tmp_path / "repo"
    outside = tmp_path / "outside.txt"
    workdir.mkdir()
    outside.write_text("do not touch\n", encoding="utf-8")

    rc = runner.main([
        "--workdir", str(workdir),
        "--name", "reject-outside",
        "--snapshot", str(outside),
        "--apply-json", json.dumps([sys.executable, "-c", "print('unused')"]),
        "--report", str(workdir / "report.json"),
    ])

    assert rc == 2
    assert outside.read_text(encoding="utf-8") == "do not touch\n"
    report = read_report(workdir / "report.json")
    assert report["status"] == "preflight_failed"
    assert "outside workdir" in report["error"]


def test_preflight_rejects_shell_string_commands_and_git_snapshots(tmp_path):
    runner = load_runner()
    workdir = tmp_path / "repo"
    git_config = workdir / ".git" / "config"
    git_config.parent.mkdir(parents=True)
    git_config.write_text("[core]\n", encoding="utf-8")

    rc = runner.main([
        "--workdir", str(workdir),
        "--name", "reject-shell-and-git",
        "--snapshot", ".git/config",
        "--apply-json", json.dumps("echo unused"),
        "--report", str(workdir / "report.json"),
    ])

    assert rc == 2
    assert git_config.read_text(encoding="utf-8") == "[core]\n"
    report = read_report(workdir / "report.json")
    assert report["status"] == "preflight_failed"
    assert ".git" in report["error"] or "argv list" in report["error"]
