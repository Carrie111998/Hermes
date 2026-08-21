from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


SCRIPT = (
    Path(__file__).parents[2]
    / "optional-skills"
    / "autonomous-ai-agents"
    / "grok"
    / "scripts"
    / "hermes_kanban_worker.py"
)
SPEC = importlib.util.spec_from_file_location("hermes_kanban_worker", SCRIPT)
assert SPEC and SPEC.loader
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)
FAKE_ACP_AGENT = Path(__file__).parent / "fixtures" / "fake_acp_agent.py"


def run_fake_acp(tmp_path: Path, *agent_args: str, timeout: float = 10):
    return worker.run_grok_acp(
        [sys.executable, str(FAKE_ACP_AGENT), *agent_args],
        env={},
        cwd=tmp_path,
        prompt="Exercise the ACP transport boundary.",
        timeout=timeout,
        no_progress_timeout=0,
        progress_probe=lambda: False,
        poll_interval=0.005,
    )


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def report(status: str = "completed") -> dict:
    return {
        "status": status,
        "summary": "Implemented and verified the card.",
        "changed_files": ["src/example.py"],
        "tests": [{"command": "pytest -q", "outcome": "passed", "details": "1 passed"}],
        "risks": [],
        "evidence": ["pytest -q: 1 passed"],
        "block_reason": "Need an unavailable dependency."
        if status == "blocked"
        else "",
        "block_kind": "dependency" if status == "blocked" else None,
    }


def require_structured_summary(value: dict) -> None:
    if "summary" not in value:
        raise worker.AdapterError("missing summary")


def args(
    tmp_path: Path,
    *,
    transport: str = "headless",
    reviewer: str | None = "",
):
    if not (tmp_path / ".git").exists():
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "-c",
                "user.name=Adapter Tests",
                "-c",
                "user.email=adapter@example.invalid",
                "commit",
                "--allow-empty",
                "-q",
                "-m",
                "baseline",
            ],
            check=True,
        )
        (tmp_path / "src").mkdir(exist_ok=True)
        (tmp_path / "src" / "example.py").write_text("# implementation diff\n")
    argv = [
        "task-1",
        "--board",
        "adapter-test",
        "--workspace",
        str(tmp_path),
        "--transport",
        transport,
        "--acceptance-command",
        json.dumps({
            "label": "adapter-test",
            "argv": [sys.executable, "-c", "raise SystemExit(0)"],
            "timeout": 10,
        }),
    ]
    if transport == "headless":
        argv.append("--allow-experimental-headless")
    if reviewer is not None:
        argv.extend(["--reviewer", reviewer])
    return worker.build_parser().parse_args(argv)


class FakeHermes:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, argv, *, env, cwd=None, timeout=None):
        self.calls.append((list(argv), dict(env)))
        command = argv[4]
        stdout = ""
        if command == "show":
            stdout = json.dumps({
                "task": {"id": "task-1", "assignee": "worker-grok-cli"}
            })
        elif command == "claim":
            stdout = f"Claimed task-1\nWorkspace: {self.workspace}\n"
        elif command == "context":
            stdout = "Title: Implement the example\nBody: Run tests.\n"
        return subprocess.CompletedProcess(argv, 0, stdout, "")


def terminal_commands(fake: FakeHermes) -> list[list[str]]:
    return [
        call
        for call, _env in fake.calls
        if call[4] in {"complete", "request-review", "block"}
    ]


def test_child_environment_resolves_symlinked_grok_home_for_sandbox(tmp_path):
    real_grok_home = tmp_path / "real-grok-home"
    real_grok_home.mkdir()
    linked_grok_home = tmp_path / ".grok"
    linked_grok_home.symlink_to(real_grok_home, target_is_directory=True)

    child = worker.child_environment({"HOME": str(tmp_path)})

    assert child["GROK_HOME"] == str(real_grok_home.resolve())


def test_headless_requires_explicit_experimental_opt_in(tmp_path, capsys):
    parsed = worker.build_parser().parse_args([
        "task-1",
        "--board",
        "adapter-test",
        "--workspace",
        str(tmp_path),
        "--transport",
        "headless",
    ])
    calls = []

    def command_runner(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("headless policy must fail before Hermes commands")

    assert worker.execute(parsed, command_runner=command_runner) == 1
    assert calls == []
    assert "explicit experimental opt-in" in capsys.readouterr().err


def test_completed_report_always_completes_card(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGHTCODE_API_KEY", "test-value-not-printed")
    monkeypatch.delenv("RIGHTCODE_GROK_API_KEY", raising=False)
    hermes = FakeHermes(tmp_path)
    grok_calls = []
    parsed = args(tmp_path)
    (tmp_path / "preexisting-unrelated.txt").write_text("leave untouched\n")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    run_tests = scripts / "run_tests.sh"
    run_tests.write_text("#!/usr/bin/env bash\nexit 0\n")
    run_tests.chmod(0o755)

    def inspect(argv, *, env, cwd=None, timeout=None):
        assert argv[-2:] == ["inspect", "--json"]
        assert cwd == tmp_path.resolve()
        payload = {
            "grokVersion": "1.0.4",
            "projectTrusted": True,
            "projectInstructions": [{"path": "AGENTS.md"}],
            "skills": [],
            "plugins": [],
            "mcpServers": [],
            "lspServers": [],
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    def grok(argv, *, env, cwd, timeout):
        grok_calls.append((list(argv), dict(env), cwd))
        prompt = argv[argv.index("-p") + 1]
        assert "HERMES PROJECT CONTEXT PACK" in prompt
        assert (
            "Hermes-detected verification commands:\n- scripts/run_tests.sh" in prompt
        )
        assert "project_instructions=1, skills=0" in prompt
        (cwd / "src" / "example.py").write_text("# change made by this run\n")
        return json.dumps({"result": json.dumps(report())})

    assert (
        worker.execute(
            parsed,
            command_runner=hermes,
            grok_runner=grok,
            inspect_runner=inspect,
        )
        == 0
    )
    terminals = terminal_commands(hermes)
    assert len(terminals) == 1
    assert terminals[0][4] == "complete"
    metadata = json.loads(terminals[0][terminals[0].index("--metadata") + 1])
    assert metadata["adapter"] == "worker-grok-cli"
    assert metadata["changed_files"] == ["src/example.py"]
    assert metadata["observed_run_changes"] == ["src/example.py"]
    assert metadata["preexisting_workspace_changes"] == [
        "preexisting-unrelated.txt",
        "scripts/run_tests.sh",
        "src/example.py",
    ]
    assert metadata["observed_workspace_changes"] == [
        "preexisting-unrelated.txt",
        "scripts/run_tests.sh",
        "src/example.py",
    ]
    assert metadata["project_context"]["verify_commands"] == ["scripts/run_tests.sh"]
    assert metadata["grok_discovery"]["project_instructions"] == 1
    execution = metadata["adapter_execution"]
    assert execution["classification"] == "completed"
    assert execution["terminal_action"] == "complete"
    assert execution["run_id"]
    assert [phase["name"] for phase in execution["phases"]] == [
        "policy",
        "task_lookup",
        "claim",
        "workspace_readiness",
        "project_context",
        "work_report",
        "verification",
        "terminal",
    ]
    assert all(phase["duration_ms"] >= 0 for phase in execution["phases"])
    assert grok_calls[0][1]["RIGHTCODE_GROK_API_KEY"] == "test-value-not-printed"
    assert grok_calls[0][1]["XAI_API_KEY"] == "test-value-not-printed"
    assert grok_calls[0][1]["GROK_MODELS_BASE_URL"] == "https://rightapi.ai/grok/v1"
    assert grok_calls[0][1]["HERMES_PROFILE"] == "worker-grok-cli"
    assert all("RIGHTCODE_API_KEY" not in env for _call, env in hermes.calls)
    assert all("RIGHTCODE_GROK_API_KEY" not in env for _call, env in hermes.calls)
    assert all("XAI_API_KEY" not in env for _call, env in hermes.calls)
    assert "--session-id" in grok_calls[0][0]
    assert "--resume" not in grok_calls[0][0]
    rules = grok_calls[0][0][grok_calls[0][0].index("--rules") + 1]
    normalized_rules = " ".join(rules.split())
    assert "Editing files inside the supplied cwd" in normalized_rules
    assert "delegated to the Foreman" in normalized_rules
    assert "Follow every other applicable repository instruction" in normalized_rules


def test_supervised_completion_requests_luna_review_with_current_run_evidence(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RIGHTCODE_API_KEY", "test-value-not-printed")

    class ReviewHermes(FakeHermes):
        def __init__(self, workspace):
            super().__init__(workspace)
            self.show_count = 0

        def __call__(self, argv, *, env, cwd=None, timeout=None):
            if argv[4] == "show":
                self.show_count += 1
                self.calls.append((list(argv), dict(env)))
                payload = {
                    "task": {
                        "id": "task-1",
                        "assignee": "worker-grok-cli",
                        "status": "ready" if self.show_count == 1 else "running",
                        "current_run_id": None if self.show_count == 1 else 41,
                    }
                }
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
            return super().__call__(argv, env=env, cwd=cwd, timeout=timeout)

    hermes = ReviewHermes(tmp_path)

    def grok(argv, *, env, cwd, timeout):
        (cwd / "src" / "example.py").write_text(
            "# implementation awaiting Luna review\n", encoding="utf-8"
        )
        return json.dumps({"result": report()})

    assert (
        worker.execute(
            args(tmp_path, reviewer=None),
            command_runner=hermes,
            grok_runner=grok,
        )
        == 0
    )

    terminals = terminal_commands(hermes)
    assert len(terminals) == 1
    terminal = terminals[0]
    assert terminal[4] == "request-review"
    assert terminal[terminal.index("--reviewer") + 1] == "worker-luna"
    assert not any(call[4] == "complete" for call, _env in hermes.calls)
    terminal_env = next(env for call, env in hermes.calls if call is terminal)
    assert terminal_env["HERMES_KANBAN_TASK"] == "task-1"
    assert terminal_env["HERMES_KANBAN_RUN_ID"] == "41"
    assert "RIGHTCODE_API_KEY" not in terminal_env
    metadata = json.loads(terminal[terminal.index("--metadata") + 1])
    assert metadata["verification"]["status"] == "passed"
    assert metadata["adapter_execution"]["terminal_action"] == "request_review"


def test_supervised_completion_is_idempotent_after_concurrent_review_state(
    tmp_path, capsys
):
    class ConcurrentReviewHermes(FakeHermes):
        def __init__(self, workspace):
            super().__init__(workspace)
            self.show_count = 0

        def __call__(self, argv, *, env, cwd=None, timeout=None):
            if argv[4] == "show":
                self.show_count += 1
                self.calls.append((list(argv), dict(env)))
                payload = {
                    "task": {
                        "id": "task-1",
                        "assignee": (
                            "worker-grok-cli" if self.show_count == 1 else "worker-luna"
                        ),
                        "status": "ready" if self.show_count == 1 else "review",
                        "current_run_id": None,
                    }
                }
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
            return super().__call__(argv, env=env, cwd=cwd, timeout=timeout)

    hermes = ConcurrentReviewHermes(tmp_path)

    def grok(argv, *, env, cwd, timeout):
        (cwd / "src" / "example.py").write_text(
            "# implementation concurrently handed off\n", encoding="utf-8"
        )
        return json.dumps({"result": report()})

    assert (
        worker.execute(
            args(tmp_path, reviewer="worker-luna"),
            command_runner=hermes,
            grok_runner=grok,
        )
        == 0
    )
    assert terminal_commands(hermes) == []
    result = json.loads(capsys.readouterr().out)
    assert result["adapter_execution"]["terminal_action"] == "already_requested_review"
    assert result["adapter_execution"]["terminal_observed_status"] == "review"


def test_supervised_completion_rejects_concurrent_different_reviewer(tmp_path, capsys):
    class ConflictingReviewHermes(FakeHermes):
        def __init__(self, workspace):
            super().__init__(workspace)
            self.show_count = 0

        def __call__(self, argv, *, env, cwd=None, timeout=None):
            if argv[4] == "show":
                self.show_count += 1
                self.calls.append((list(argv), dict(env)))
                payload = {
                    "task": {
                        "id": "task-1",
                        "assignee": (
                            "worker-grok-cli"
                            if self.show_count == 1
                            else "different-reviewer"
                        ),
                        "status": "ready" if self.show_count == 1 else "review",
                        "current_run_id": None,
                    }
                }
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
            return super().__call__(argv, env=env, cwd=cwd, timeout=timeout)

    hermes = ConflictingReviewHermes(tmp_path)

    def grok(argv, *, env, cwd, timeout):
        (cwd / "src" / "example.py").write_text(
            "# implementation raced with another reviewer\n", encoding="utf-8"
        )
        return json.dumps({"result": report()})

    assert (
        worker.execute(
            args(tmp_path, reviewer=None),
            command_runner=hermes,
            grok_runner=grok,
        )
        == 1
    )
    assert terminal_commands(hermes) == []
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "failed"
    assert result["adapter_execution"]["classification"] == "terminal_conflict"
    assert result["adapter_execution"]["terminal_action"] == "conflict"


def test_default_implementation_reviewer_is_luna(tmp_path):
    parsed = worker.build_parser().parse_args([
        "task-1",
        "--board",
        "adapter-test",
        "--workspace",
        str(tmp_path),
    ])

    assert parsed.reviewer == "worker-luna"


def test_supervised_completion_fails_closed_without_current_run_id(tmp_path, capsys):
    hermes = FakeHermes(tmp_path)

    def grok(argv, *, env, cwd, timeout):
        (cwd / "src" / "example.py").write_text(
            "# implementation cannot prove claim ownership\n", encoding="utf-8"
        )
        return json.dumps({"result": report()})

    assert (
        worker.execute(
            args(tmp_path, reviewer=None),
            command_runner=hermes,
            grok_runner=grok,
        )
        == 1
    )
    terminals = terminal_commands(hermes)
    assert [command[4] for command in terminals] == ["block"]
    assert terminals[0][-1] == (
        "Hermes terminal preflight did not expose the current run id"
    )
    result = json.loads(capsys.readouterr().out)
    assert result["adapter_execution"]["classification"] == "transient"
    assert result["adapter_execution"]["terminal_action"] == "block"


def test_supervised_same_card_lifecycle_routes_changes_back_to_grok_then_approves(
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parsed = args(workspace, reviewer=None)
    scripts = workspace / "scripts"
    scripts.mkdir()
    run_tests = scripts / "run_tests.sh"
    run_tests.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    run_tests.chmod(0o755)
    state_root = tmp_path / "state"
    state_root.mkdir()
    conn = kb.connect(state_root / "kanban.db")
    task_id = kb.create_task(
        conn,
        title="Implement under Luna supervision",
        body="Make the requested change and preserve verification evidence.",
        assignee="worker-grok-cli",
        workspace_kind="dir",
        workspace_path=str(workspace),
    )

    class DomainHermes:
        def __init__(self):
            self.calls: list[tuple[list[str], dict[str, str]]] = []

        @staticmethod
        def option(argv, name):
            return argv[argv.index(name) + 1]

        def __call__(self, argv, *, env, cwd=None, timeout=None):
            del cwd, timeout
            self.calls.append((list(argv), dict(env)))
            command = argv[4]
            task = kb.get_task(conn, task_id)
            assert task is not None
            if command == "show":
                payload = {
                    "task": {
                        "id": "task-1",
                        "assignee": task.assignee,
                        "status": task.status,
                        "current_run_id": task.current_run_id,
                    }
                }
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
            if command == "claim":
                claimed = kb.claim_task(
                    conn,
                    task_id,
                    ttl_seconds=int(self.option(argv, "--ttl")),
                )
                assert claimed is not None
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    f"Claimed task-1\nWorkspace: {workspace}\n",
                    "",
                )
            if command == "context":
                return subprocess.CompletedProcess(
                    argv, 0, kb.build_worker_context(conn, task_id), ""
                )
            if command == "request-review":
                assert env["HERMES_KANBAN_TASK"] == "task-1"
                ok = kb.request_review(
                    conn,
                    task_id,
                    summary=self.option(argv, "--summary"),
                    reviewer=self.option(argv, "--reviewer"),
                    metadata=json.loads(self.option(argv, "--metadata")),
                    expected_run_id=int(env["HERMES_KANBAN_RUN_ID"]),
                )
                return subprocess.CompletedProcess(
                    argv,
                    0 if ok else 1,
                    "Requested review\n" if ok else "",
                    "" if ok else "request review failed",
                )
            raise AssertionError(f"unexpected Hermes command: {command}")

    hermes = DomainHermes()
    attempt = 0

    def grok(argv, *, env, cwd, timeout):
        nonlocal attempt
        del argv, env, timeout
        attempt += 1
        (cwd / "src" / "example.py").write_text(
            f"# Grok implementation attempt {attempt}\n", encoding="utf-8"
        )
        value = report()
        value["summary"] = f"Implementation attempt {attempt} verified."
        return json.dumps({"result": value})

    try:
        assert (
            worker.execute(
                parsed,
                command_runner=hermes,
                grok_runner=grok,
            )
            == 0
        )
        awaiting_review = kb.get_task(conn, task_id)
        assert awaiting_review is not None
        assert awaiting_review.status == "review"
        assert awaiting_review.assignee == "worker-luna"
        first_run = kb.latest_run(conn, task_id)
        assert first_run is not None
        assert first_run.outcome == "review_requested"
        assert first_run.metadata["verification"]["status"] == "passed"
        assert first_run.metadata["adapter_execution"]["terminal_action"] == (
            "request_review"
        )

        luna_review = kb.claim_review_task(
            conn, task_id, claimer="worker-luna:first-review"
        )
        assert luna_review is not None
        assert kb.request_changes(
            conn,
            task_id,
            reason="Add the missing regression evidence.",
            expected_run_id=luna_review.current_run_id,
        ) == (True, "worker-grok-cli")
        rework = kb.get_task(conn, task_id)
        assert rework is not None
        assert rework.status == "ready"
        assert rework.assignee == "worker-grok-cli"

        assert (
            worker.execute(
                args(workspace, reviewer=None),
                command_runner=hermes,
                grok_runner=grok,
            )
            == 0
        )
        rereview = kb.get_task(conn, task_id)
        assert rereview is not None
        assert rereview.status == "review"
        assert rereview.assignee == "worker-luna"
        luna_review_2 = kb.claim_review_task(
            conn, task_id, claimer="worker-luna:second-review"
        )
        assert luna_review_2 is not None
        assert kb.complete_task(
            conn,
            task_id,
            summary="Luna approved the independently verified implementation.",
            expected_run_id=luna_review_2.current_run_id,
        )
        completed = kb.get_task(conn, task_id)
        assert completed is not None
        assert completed.status == "done"
        assert completed.block_recurrences == 0
    finally:
        conn.close()


def test_terminal_completion_is_idempotent_after_concurrent_done_state(
    tmp_path, capsys
):
    class ConcurrentDoneHermes(FakeHermes):
        def __init__(self, workspace):
            super().__init__(workspace)
            self.show_count = 0

        def __call__(self, argv, *, env, cwd=None, timeout=None):
            if argv[4] == "show":
                self.show_count += 1
                self.calls.append((list(argv), dict(env)))
                status = "running" if self.show_count == 1 else "done"
                payload = {
                    "task": {
                        "id": "task-1",
                        "assignee": "worker-grok-cli",
                        "status": status,
                    }
                }
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
            return super().__call__(argv, env=env, cwd=cwd, timeout=timeout)

    hermes = ConcurrentDoneHermes(tmp_path)

    def grok(argv, *, env, cwd, timeout):
        (cwd / "src" / "example.py").write_text(
            "# completed before terminal transition\n", encoding="utf-8"
        )
        return json.dumps({"result": report()})

    assert worker.execute(args(tmp_path), command_runner=hermes, grok_runner=grok) == 0
    assert hermes.show_count == 2
    assert terminal_commands(hermes) == []
    result = json.loads(capsys.readouterr().out)
    assert result["adapter_execution"]["terminal_action"] == "already_complete"
    assert result["adapter_execution"]["terminal_observed_status"] == "done"


def test_acp_work_and_terminal_report_use_one_runner_without_headless(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RIGHTCODE_API_KEY", "test-value-not-printed")
    hermes = FakeHermes(tmp_path)
    parsed = args(tmp_path, transport="acp")
    acp_calls = []

    def acp(
        argv,
        *,
        env,
        cwd,
        prompt,
        timeout,
        no_progress_timeout,
        progress_probe,
        report_prompt_factory,
        report_schema,
        report_validator,
        report_timeout,
    ):
        acp_calls.append((list(argv), dict(env), cwd, prompt, timeout))
        assert argv[-1] == "stdio"
        assert argv[:2] == [parsed.grok_bin, "agent"]
        assert "--always-approve" in argv
        assert "--no-leader" in argv
        assert "--json-schema" not in argv
        assert "HERMES PROJECT CONTEXT PACK" in prompt
        assert "status=working" not in prompt
        assert no_progress_timeout == parsed.no_progress_timeout
        assert progress_probe() is False
        (cwd / "src" / "example.py").write_text("# changed through ACP\n")
        report_prompt = report_prompt_factory("end_turn", None)
        assert '"src/example.py"' in report_prompt
        assert report_schema["properties"]["status"]["enum"] == [
            "completed",
            "blocked",
        ]
        assert report_timeout == parsed.correction_timeout
        terminal_report = report()
        report_validator(terminal_report)
        return worker.AcpWorkResult(
            session_id="11111111-1111-4111-8111-111111111111",
            stop_reason="end_turn",
            update_count=9,
            tool_call_count=3,
            report=terminal_report,
        )

    def grok(*_args, **_kwargs):
        raise AssertionError("ACP transport must not launch a headless report process")

    assert (
        worker.execute(
            parsed,
            command_runner=hermes,
            acp_runner=acp,
            grok_runner=grok,
        )
        == 0
    )
    assert len(acp_calls) == 1
    terminal = terminal_commands(hermes)[0]
    metadata = json.loads(terminal[terminal.index("--metadata") + 1])
    assert metadata["transport"] == "acp"
    assert metadata["acp"] == {
        "max_update_json_bytes": 0,
        "stop_reason": "end_turn",
        "tool_call_count": 3,
        "update_count": 9,
    }


def test_acp_adapter_runs_fixed_verification_and_injects_real_evidence(
    tmp_path, monkeypatch
):
    hermes = FakeHermes(tmp_path)
    parsed = args(tmp_path, transport="acp")
    marker = tmp_path.parent / "adapter-verification-ran"
    verify_script = tmp_path / "verify_marker.py"
    verify_script.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "Path(sys.argv[1]).write_text('adapter-ran\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    command = f'{sys.executable} "{verify_script}" "{marker}"'
    parsed.acceptance_command = [
        json.dumps({
            "label": "marker",
            "argv": [sys.executable, str(verify_script), str(marker)],
            "timeout": 10,
        })
    ]
    monkeypatch.setattr(
        worker,
        "build_project_context_pack",
        lambda _workspace: {
            "workspace_snapshot": "fixture",
            "root": str(tmp_path),
            "manifests": [],
            "verify_commands": [command],
            "context_files": [],
        },
    )

    def acp(_argv, **kwargs):
        (kwargs["cwd"] / "src" / "example.py").write_text(
            "# changed through ACP\n", encoding="utf-8"
        )
        kwargs["report_prompt_factory"]("end_turn", None)
        candidate = report()
        kwargs["report_validator"](candidate)
        return worker.AcpWorkResult(
            session_id="33333333-3333-4333-8333-333333333333",
            stop_reason="end_turn",
            update_count=1,
            tool_call_count=1,
            report=candidate,
        )

    assert worker.execute(parsed, command_runner=hermes, acp_runner=acp) == 0
    assert marker.read_text(encoding="utf-8") == "adapter-ran\n"
    terminal = terminal_commands(hermes)[0]
    metadata = json.loads(terminal[terminal.index("--metadata") + 1])
    verification = metadata["verification"]
    assert verification["commands"][0]["argv"] == [
        sys.executable,
        str(verify_script),
        str(marker),
    ]
    assert verification["commands"][0]["returncode"] == 0
    assert verification["commands"][0]["timed_out"] is False
    assert verification["commands"][0]["stdout"]["sha256"]
    assert verification["commands"][0]["stderr"]["sha256"]


def test_adapter_verification_workspace_mutation_blocks_completion(
    tmp_path, monkeypatch, capsys
):
    hermes = FakeHermes(tmp_path)
    parsed = args(tmp_path, transport="acp")
    verify_script = tmp_path / "verify_mutation.py"
    verify_script.write_text(
        "from pathlib import Path\n"
        "Path('verify-mutated.txt').write_text('unexpected\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    command = f'{sys.executable} "{verify_script}"'
    parsed.acceptance_command = [
        json.dumps({
            "label": "mutation",
            "argv": [sys.executable, str(verify_script)],
            "timeout": 10,
        })
    ]
    monkeypatch.setattr(
        worker,
        "build_project_context_pack",
        lambda _workspace: {
            "workspace_snapshot": "fixture",
            "root": str(tmp_path),
            "manifests": [],
            "verify_commands": [command],
            "context_files": [],
        },
    )

    def acp(_argv, **kwargs):
        (kwargs["cwd"] / "src" / "example.py").write_text(
            "# changed through ACP\n", encoding="utf-8"
        )
        kwargs["report_prompt_factory"]("end_turn", None)
        candidate = report()
        kwargs["report_validator"](candidate)
        return worker.AcpWorkResult(
            session_id="34343434-3434-4434-8434-343434343434",
            stop_reason="end_turn",
            update_count=1,
            tool_call_count=1,
            report=candidate,
        )

    assert worker.execute(parsed, command_runner=hermes, acp_runner=acp) == 0

    terminal = terminal_commands(hermes)[0]
    assert terminal[4:7] == ["block", "--kind", "transient"]
    assert "modified the workspace" in terminal[-1]
    result = json.loads(capsys.readouterr().out)
    assert result["verification"]["status"] == "failed"
    assert result["verification"]["workspace_mutations"] == ["verify-mutated.txt"]
    assert not (tmp_path / "verify-mutated.txt").exists()
    assert "verify-mutated.txt" not in result["observed_workspace_changes"]


def test_adapter_verification_git_head_change_blocks_completion(
    tmp_path, monkeypatch, capsys
):
    hermes = FakeHermes(tmp_path)
    parsed = args(tmp_path, transport="acp")
    source_head_before = worker.workspace_head(tmp_path)
    verify_script = tmp_path / "verify_commit.py"
    verify_script.write_text(
        "from pathlib import Path\n"
        "import subprocess\n"
        "Path('verify-committed.txt').write_text('unexpected\\n', encoding='utf-8')\n"
        "subprocess.run(['git', 'add', 'verify-committed.txt'], check=True)\n"
        "subprocess.run([\n"
        "    'git', '-c', 'user.name=Adapter Tests',\n"
        "    '-c', 'user.email=adapter@example.invalid',\n"
        "    'commit', '-q', '-m', 'forbidden verification commit',\n"
        "], check=True)\n",
        encoding="utf-8",
    )
    command = f'{sys.executable} "{verify_script}"'
    parsed.acceptance_command = [
        json.dumps({
            "label": "head-change",
            "argv": [sys.executable, str(verify_script)],
            "timeout": 10,
        })
    ]
    monkeypatch.setattr(
        worker,
        "build_project_context_pack",
        lambda _workspace: {
            "workspace_snapshot": "fixture",
            "root": str(tmp_path),
            "manifests": [],
            "verify_commands": [command],
            "context_files": [],
        },
    )

    def acp(_argv, **kwargs):
        (kwargs["cwd"] / "src" / "example.py").write_text(
            "# changed through ACP\n", encoding="utf-8"
        )
        kwargs["report_prompt_factory"]("end_turn", None)
        candidate = report()
        kwargs["report_validator"](candidate)
        return worker.AcpWorkResult(
            session_id="35353535-3535-4535-8535-353535353535",
            stop_reason="end_turn",
            update_count=1,
            tool_call_count=1,
            report=candidate,
        )

    assert worker.execute(parsed, command_runner=hermes, acp_runner=acp) == 0

    terminal = terminal_commands(hermes)[0]
    assert terminal[4:7] == ["block", "--kind", "transient"]
    assert "changed Git HEAD" in terminal[-1]
    result = json.loads(capsys.readouterr().out)
    assert result["verification"]["status"] == "failed"
    assert result["verification"]["head_changed"] is True
    assert result["verification"]["head_before"]
    assert result["verification"]["head_after"]
    assert worker.workspace_head(tmp_path) == source_head_before
    assert not (tmp_path / "verify-committed.txt").exists()


def test_adapter_verification_failure_overrides_model_completion(
    tmp_path, monkeypatch, capsys
):
    hermes = FakeHermes(tmp_path)
    parsed = args(tmp_path, transport="acp")
    verify_script = tmp_path / "verify_failure.py"
    verify_script.write_text(
        "import sys\n"
        "print('PROVIDER_TOKEN=super-secret-value')\n"
        "print('verification failed', file=sys.stderr)\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    command = f'{sys.executable} "{verify_script}"'
    parsed.acceptance_command = [
        json.dumps({
            "label": "failure",
            "argv": [sys.executable, str(verify_script)],
            "timeout": 10,
        })
    ]
    monkeypatch.setattr(
        worker,
        "build_project_context_pack",
        lambda _workspace: {
            "workspace_snapshot": "fixture",
            "root": str(tmp_path),
            "manifests": [],
            "verify_commands": [command],
            "context_files": [],
        },
    )

    def acp(_argv, **kwargs):
        (kwargs["cwd"] / "src" / "example.py").write_text(
            "# changed through ACP\n", encoding="utf-8"
        )
        kwargs["report_prompt_factory"]("end_turn", None)
        candidate = report()
        kwargs["report_validator"](candidate)
        return worker.AcpWorkResult(
            session_id="44444444-4444-4444-8444-444444444444",
            stop_reason="end_turn",
            update_count=1,
            tool_call_count=1,
            report=candidate,
        )

    assert worker.execute(parsed, command_runner=hermes, acp_runner=acp) == 0
    output = capsys.readouterr().out
    assert "super-secret-value" not in output
    assert '"status": "blocked"' in output
    terminal = terminal_commands(hermes)[0]
    assert terminal[4:7] == ["block", "--kind", "transient"]
    assert "exited with code 7" in terminal[-1]


def test_adapter_verification_timeout_is_reported_without_full_output(
    tmp_path, monkeypatch, capsys
):
    hermes = FakeHermes(tmp_path)
    parsed = args(tmp_path, transport="acp")
    parsed.command_timeout = 0.05
    verify_script = tmp_path / "verify_timeout.py"
    verify_script.write_text(
        "import time\nprint('timeout-output')\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    command = f'{sys.executable} "{verify_script}"'
    parsed.acceptance_command = [
        json.dumps({
            "label": "timeout",
            "argv": [sys.executable, str(verify_script)],
            "timeout": 10,
        })
    ]
    monkeypatch.setattr(
        worker,
        "build_project_context_pack",
        lambda _workspace: {
            "workspace_snapshot": "fixture",
            "root": str(tmp_path),
            "manifests": [],
            "verify_commands": [command],
            "context_files": [],
        },
    )

    def acp(_argv, **kwargs):
        (kwargs["cwd"] / "src" / "example.py").write_text(
            "# changed through ACP\n", encoding="utf-8"
        )
        kwargs["report_prompt_factory"]("end_turn", None)
        candidate = report()
        kwargs["report_validator"](candidate)
        return worker.AcpWorkResult(
            session_id="55555555-5555-4555-8555-555555555555",
            stop_reason="end_turn",
            update_count=1,
            tool_call_count=1,
            report=candidate,
        )

    assert worker.execute(parsed, command_runner=hermes, acp_runner=acp) == 0
    output = capsys.readouterr().out
    assert '"timed_out": true' in output
    assert terminal_commands(hermes)[0][4:7] == ["block", "--kind", "transient"]


def test_adapter_verification_rejects_implicit_shell_operators(tmp_path, monkeypatch):
    hermes = FakeHermes(tmp_path)
    parsed = args(tmp_path, transport="acp")
    marker = tmp_path.parent / "implicit-shell-ran"
    first_script = tmp_path / "verify_first.py"
    second_script = tmp_path / "verify_second.py"
    first_script.write_text("raise SystemExit(0)\n", encoding="utf-8")
    second_script.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "Path(sys.argv[1]).write_text('ran\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    command = (
        f'{sys.executable} "{first_script}" && '
        f'{sys.executable} "{second_script}" "{marker}"'
    )
    parsed.acceptance_command = [
        json.dumps({
            "label": "shell",
            "argv": [
                sys.executable,
                str(first_script),
                "&&",
                sys.executable,
                str(second_script),
                str(marker),
            ],
            "timeout": 10,
        })
    ]
    monkeypatch.setattr(
        worker,
        "build_project_context_pack",
        lambda _workspace: {
            "workspace_snapshot": "fixture",
            "root": str(tmp_path),
            "manifests": [],
            "verify_commands": [command],
            "context_files": [],
        },
    )

    def acp(_argv, **kwargs):
        (kwargs["cwd"] / "src" / "example.py").write_text(
            "# changed through ACP\n", encoding="utf-8"
        )
        kwargs["report_prompt_factory"]("end_turn", None)
        candidate = report()
        kwargs["report_validator"](candidate)
        return worker.AcpWorkResult(
            session_id="57575757-5757-4757-8757-575757575757",
            stop_reason="end_turn",
            update_count=1,
            tool_call_count=1,
            report=candidate,
        )

    assert worker.execute(parsed, command_runner=hermes, acp_runner=acp) == 1
    assert not marker.exists()
    assert terminal_commands(hermes) == []


@pytest.mark.linux_only
@pytest.mark.live_system_guard_bypass
def test_adapter_verification_reaps_detached_descendant(tmp_path, monkeypatch):
    hermes = FakeHermes(tmp_path)
    parsed = args(tmp_path, transport="acp")
    parsed.command_timeout = 5
    pid_file = tmp_path.parent / "verification-detached-child.pid"
    verify_script = tmp_path / "verify_detached_child.py"
    verify_script.write_text(
        "from pathlib import Path\n"
        "import subprocess\n"
        "import sys\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        "    stdin=subprocess.DEVNULL,\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        "    start_new_session=True,\n"
        ")\n"
        "Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8')\n",
        encoding="utf-8",
    )
    command = f'{sys.executable} "{verify_script}" "{pid_file}"'
    parsed.acceptance_command = [
        json.dumps({
            "label": "cleanup",
            "argv": [sys.executable, str(verify_script), str(pid_file)],
            "timeout": 5,
        })
    ]
    monkeypatch.setattr(
        worker,
        "build_project_context_pack",
        lambda _workspace: {
            "workspace_snapshot": "fixture",
            "root": str(tmp_path),
            "manifests": [],
            "verify_commands": [command],
            "context_files": [],
        },
    )

    def acp(_argv, **kwargs):
        (kwargs["cwd"] / "src" / "example.py").write_text(
            "# changed through ACP\n", encoding="utf-8"
        )
        kwargs["report_prompt_factory"]("end_turn", None)
        candidate = report()
        kwargs["report_validator"](candidate)
        return worker.AcpWorkResult(
            session_id="56565656-5656-4656-8656-565656565656",
            stop_reason="end_turn",
            update_count=1,
            tool_call_count=1,
            report=candidate,
        )

    child_pid = None
    try:
        assert worker.execute(parsed, command_runner=hermes, acp_runner=acp) == 0
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        for _attempt in range(100):
            if not process_exists(child_pid):
                break
            threading.Event().wait(0.02)
        assert not process_exists(child_pid)
        assert terminal_commands(hermes)[0][4] == "complete"
    finally:
        if child_pid is not None and process_exists(child_pid):
            os.kill(child_pid, signal.SIGKILL)


@pytest.mark.linux_only
@pytest.mark.live_system_guard_bypass
def test_adapter_run_reaps_detached_model_descendant(tmp_path):
    hermes = FakeHermes(tmp_path)
    pid_file = tmp_path.parent / "model-detached-child.pid"
    child_pid = None

    def grok(argv, *, env, cwd, timeout):
        nonlocal child_pid
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import subprocess, sys; "
                "child=subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(60)'], stdin=subprocess.DEVNULL, "
                "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
                "start_new_session=True); Path(sys.argv[1]).write_text(str(child.pid))",
                str(pid_file),
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        (cwd / "src" / "example.py").write_text(
            "# changed while detached child runs\n", encoding="utf-8"
        )
        return json.dumps({"result": report()})

    try:
        assert (
            worker.execute(args(tmp_path), command_runner=hermes, grok_runner=grok) == 0
        )
        assert child_pid is not None
        for _attempt in range(100):
            if not process_exists(child_pid):
                break
            threading.Event().wait(0.02)
        assert not process_exists(child_pid)
    finally:
        if child_pid is not None and process_exists(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def test_acp_no_progress_watchdog_cancels_the_live_session(tmp_path, monkeypatch):
    cancelled = []

    class Connection:
        async def initialize(self, protocol_version):
            assert protocol_version == 1

        async def new_session(self, cwd, mcp_servers):
            assert cwd == str(tmp_path)
            assert mcp_servers == []
            return types.SimpleNamespace(
                session_id="22222222-2222-4222-8222-222222222222"
            )

        async def prompt(self, prompt, session_id):
            await asyncio.Event().wait()

        async def cancel(self, session_id):
            cancelled.append(session_id)

    connection = Connection()

    @contextlib.asynccontextmanager
    async def spawn_agent_process(client, command, *argv, **kwargs):
        yield connection, types.SimpleNamespace(stderr=None)

    fake_acp = types.SimpleNamespace(
        PROTOCOL_VERSION=1,
        spawn_agent_process=spawn_agent_process,
        text_block=lambda text: {"type": "text", "text": text},
    )
    monkeypatch.setitem(sys.modules, "acp", fake_acp)

    try:
        worker.run_grok_acp(
            ["grok", "agent", "--always-approve", "stdio"],
            env={},
            cwd=tmp_path,
            prompt="Implement the card.",
            timeout=1,
            no_progress_timeout=0.01,
            progress_probe=lambda: False,
            poll_interval=0.001,
        )
    except worker.GrokNoProgress as exc:
        assert "no workspace changes" in str(exc)
    else:
        raise AssertionError("expected GrokNoProgress")

    assert cancelled == ["22222222-2222-4222-8222-222222222222"]


def test_acp_accepts_a_large_single_line_json_rpc_frame(tmp_path, monkeypatch):
    frame = (
        b'{"jsonrpc":"2.0","method":"session/update","params":"'
        + (b"x" * (256 * 1024))
        + b'"}\n'
    )

    class Connection:
        async def initialize(self, protocol_version):
            assert protocol_version == 1

        async def new_session(self, cwd, mcp_servers):
            assert cwd == str(tmp_path)
            assert mcp_servers == []
            return types.SimpleNamespace(
                session_id="55555555-5555-4555-8555-555555555555"
            )

        async def prompt(self, prompt, session_id):
            del prompt, session_id
            return types.SimpleNamespace(stop_reason="end_turn")

    @contextlib.asynccontextmanager
    async def spawn_agent_process(client, command, *argv, **kwargs):
        del client, command, argv
        limit = (kwargs.get("transport_kwargs") or {}).get("limit")
        reader = asyncio.StreamReader(limit=limit) if limit else asyncio.StreamReader()
        reader.feed_data(frame)
        reader.feed_eof()
        assert await reader.readline() == frame
        yield Connection(), types.SimpleNamespace(stderr=None)

    fake_acp = types.SimpleNamespace(
        PROTOCOL_VERSION=1,
        spawn_agent_process=spawn_agent_process,
        text_block=lambda text: {"type": "text", "text": text},
    )
    monkeypatch.setitem(sys.modules, "acp", fake_acp)

    result = worker.run_grok_acp(
        ["grok", "agent", "stdio"],
        env={},
        cwd=tmp_path,
        prompt="Implement the card.",
        timeout=1,
        no_progress_timeout=0,
        progress_probe=lambda: False,
    )

    assert result.stop_reason == "end_turn"


@pytest.mark.parametrize(
    "frame_bytes", [64 * 1024, 256 * 1024, 1024 * 1024, 8 * 1024 * 1024]
)
def test_acp_official_stdio_accepts_exact_large_frame_boundaries(tmp_path, frame_bytes):
    result = run_fake_acp(tmp_path, "--frames", str(frame_bytes))

    assert result.stop_reason == "end_turn"
    assert result.update_count == 1
    assert result.update_sequence == (0,)
    assert result.max_update_json_bytes >= frame_bytes - 256


def test_acp_official_stdio_reassembles_chunked_frame(tmp_path):
    result = run_fake_acp(
        tmp_path,
        "--frames",
        str(1024 * 1024),
        "--chunk-size",
        "4093",
    )

    assert result.update_count == 1
    assert result.update_sequence == (0,)


def test_acp_official_stdio_preserves_multiple_large_frame_order(tmp_path):
    sizes = [256 * 1024, 1024 * 1024, 8 * 1024 * 1024]
    result = run_fake_acp(tmp_path, "--frames", ",".join(map(str, sizes)))

    assert result.update_count == 3
    assert result.update_sequence == (0, 1, 2)
    assert result.max_update_json_bytes >= sizes[-1] - 256


def test_acp_work_and_structured_report_share_one_official_stdio_session(tmp_path):
    prompt_log = tmp_path / "prompt-log.ndjson"

    result = worker.run_grok_acp(
        [
            sys.executable,
            str(FAKE_ACP_AGENT),
            "--structured-report",
            "--prompt-log",
            str(prompt_log),
        ],
        env={},
        cwd=tmp_path,
        prompt="Perform substantive work.",
        timeout=10,
        no_progress_timeout=0,
        progress_probe=lambda: False,
        report_prompt_factory=lambda stop_reason, error: (
            "Return the corrected structured report."
            if error
            else f"Return the structured report after {stop_reason}."
        ),
        report_schema={"type": "object"},
        report_validator=lambda value: None,
        report_timeout=10,
    )

    prompts = [json.loads(line) for line in prompt_log.read_text().splitlines()]
    assert result.report == {
        "status": "completed",
        "summary": "Structured ACP report.",
    }
    assert len(prompts) == 2
    assert {item["pid"] for item in prompts} == {prompts[0]["pid"]}
    assert {item["session_id"] for item in prompts} == {result.session_id}
    assert [item["has_output_schema"] for item in prompts] == [False, True]


def test_acp_invalid_report_gets_one_correction_in_the_same_stdio_session(tmp_path):
    prompt_log = tmp_path / "correction-prompt-log.ndjson"
    prompt_factory_calls = []

    def report_prompt_factory(stop_reason, error):
        prompt_factory_calls.append((stop_reason, str(error) if error else None))
        return "Correct the report." if error else "Return the report."

    def validate_report(value):
        if "summary" not in value:
            raise worker.AdapterError("structured summary is required")

    result = worker.run_grok_acp(
        [
            sys.executable,
            str(FAKE_ACP_AGENT),
            "--structured-report",
            "--invalid-first-report",
            "--prompt-log",
            str(prompt_log),
        ],
        env={},
        cwd=tmp_path,
        prompt="Perform substantive work.",
        timeout=10,
        no_progress_timeout=0,
        progress_probe=lambda: False,
        report_prompt_factory=report_prompt_factory,
        report_schema={"type": "object"},
        report_validator=validate_report,
        report_timeout=10,
    )

    prompts = [json.loads(line) for line in prompt_log.read_text().splitlines()]
    assert result.report["summary"] == "Structured ACP report."
    assert prompt_factory_calls == [
        ("end_turn", None),
        ("end_turn", "structured summary is required"),
    ]
    assert len(prompts) == 3
    assert {item["pid"] for item in prompts} == {prompts[0]["pid"]}
    assert {item["session_id"] for item in prompts} == {result.session_id}
    assert [item["has_output_schema"] for item in prompts] == [False, True, True]


def test_acp_residual_acceptance_gets_exactly_one_same_session_continuation(tmp_path):
    prompt_log = tmp_path / "acceptance-continuation.ndjson"
    probes = []

    def acceptance_probe():
        probes.append(len(probes) + 1)
        failed = len(probes) == 1
        return {
            "status": "failed" if failed else "passed",
            "commands": [
                {
                    "label": "ruff",
                    "argv": ["ruff", "check", "."],
                    "returncode": 1 if failed else 0,
                    "timed_out": False,
                    "stderr": worker._stderr_evidence(
                        "RUF015 residual" if failed else ""
                    ),
                }
            ],
            "workspace_mutation_count": 0,
            "head_changed": False,
        }

    result = worker.run_grok_acp(
        [
            sys.executable,
            str(FAKE_ACP_AGENT),
            "--structured-report",
            "--prompt-log",
            str(prompt_log),
        ],
        env={},
        cwd=tmp_path,
        prompt="Perform substantive work.",
        timeout=10,
        no_progress_timeout=0,
        progress_probe=lambda: False,
        report_prompt_factory=lambda stop_reason, error: f"Report after {stop_reason}",
        report_schema={"type": "object"},
        report_validator=lambda value: None,
        report_timeout=10,
        acceptance_probe=acceptance_probe,
    )

    prompts = [json.loads(line) for line in prompt_log.read_text().splitlines()]
    assert probes == [1, 2]
    assert result.acceptance["status"] == "passed"
    assert len(prompts) == 3
    assert len({item["pid"] for item in prompts}) == 1
    assert len({item["session_id"] for item in prompts}) == 1
    assert [item["has_output_schema"] for item in prompts] == [False, False, True]


def test_acp_second_residual_acceptance_failure_blocks_and_preserves_partial_diff(
    tmp_path,
):
    hermes = FakeHermes(tmp_path)
    parsed = args(tmp_path, transport="acp")
    parsed.acceptance_command = [
        json.dumps({
            "label": "ruff",
            "argv": [sys.executable, "-c", "raise SystemExit(7)"],
            "timeout": 10,
        })
    ]
    prompt_log = tmp_path.parent / f"{tmp_path.name}-residual-twice.ndjson"
    partial = "# partial ACP implementation\n"

    def acp_runner(_argv, **kwargs):
        (tmp_path / "src" / "example.py").write_text(partial, encoding="utf-8")
        return worker.run_grok_acp(
            [
                sys.executable,
                str(FAKE_ACP_AGENT),
                "--structured-report",
                "--complete-structured-report",
                "--prompt-log",
                str(prompt_log),
            ],
            **kwargs,
        )

    def no_headless(*_args, **_kwargs):
        raise AssertionError("ACP acceptance failure must not use headless fallback")

    assert (
        worker.execute(
            parsed,
            command_runner=hermes,
            grok_runner=no_headless,
            acp_runner=acp_runner,
        )
        == 0
    )

    prompts = [json.loads(line) for line in prompt_log.read_text().splitlines()]
    terminals = terminal_commands(hermes)
    assert len(prompts) == 3
    assert [item["has_output_schema"] for item in prompts] == [False, False, True]
    assert len({item["pid"] for item in prompts}) == 1
    assert len({item["session_id"] for item in prompts}) == 1
    assert len(terminals) == 1
    assert terminals[0][4:7] == ["block", "--kind", "transient"]
    assert not any(
        command[4] in {"complete", "request-review"} for command, _ in hermes.calls
    )
    assert (tmp_path / "src" / "example.py").read_text(encoding="utf-8") == partial


def test_acp_completed_report_with_block_reason_is_corrected_in_same_session(tmp_path):
    prompt_log = tmp_path / "completed-block-reason-prompt-log.ndjson"
    report_prompts = []
    validation_errors = []

    def report_prompt_factory(stop_reason, error):
        validation_errors.append(str(error) if error else None)
        prompt = worker._terminal_report_prompt(
            None,
            ["src/example.py"],
            stop_reason=stop_reason,
            validation_error=error,
        )
        report_prompts.append(prompt)
        return prompt

    result = worker.run_grok_acp(
        [
            sys.executable,
            str(FAKE_ACP_AGENT),
            "--structured-report",
            "--invalid-completed-block-reason-first-report",
            "--prompt-log",
            str(prompt_log),
        ],
        env={},
        cwd=tmp_path,
        prompt="Perform substantive work.",
        timeout=10,
        no_progress_timeout=0,
        progress_probe=lambda: False,
        report_prompt_factory=report_prompt_factory,
        report_schema=worker.REPORT_SCHEMA,
        report_validator=lambda value: worker.validate_report(
            value,
            task_mode="implementation",
        ),
        report_timeout=10,
    )

    prompts = [json.loads(line) for line in prompt_log.read_text().splitlines()]
    invariant = (
        "If status is completed, block_reason MUST be exactly an empty string "
        "and block_kind MUST be null."
    )
    assert result.report["block_reason"] == ""
    assert validation_errors == [
        None,
        "A completed worker report must have an empty block_reason",
    ]
    assert all(invariant in prompt for prompt in report_prompts)
    assert len(prompts) == 3
    assert {item["pid"] for item in prompts} == {prompts[0]["pid"]}
    assert {item["session_id"] for item in prompts} == {result.session_id}
    assert [item["has_output_schema"] for item in prompts] == [False, True, True]


def test_acp_completed_report_with_not_run_is_corrected_in_same_session(tmp_path):
    prompt_log = tmp_path / "completed-not-run-prompt-log.ndjson"
    report_prompts = []
    validation_errors = []

    def report_prompt_factory(stop_reason, error):
        validation_errors.append(str(error) if error else None)
        prompt = worker._terminal_report_prompt(
            None,
            [],
            stop_reason=stop_reason,
            validation_error=error,
        )
        report_prompts.append(prompt)
        return prompt

    result = worker.run_grok_acp(
        [
            sys.executable,
            str(FAKE_ACP_AGENT),
            "--structured-report",
            "--invalid-completed-not-run-first-report",
            "--prompt-log",
            str(prompt_log),
        ],
        env={},
        cwd=tmp_path,
        prompt="Perform substantive work.",
        timeout=10,
        no_progress_timeout=0,
        progress_probe=lambda: False,
        report_prompt_factory=report_prompt_factory,
        report_schema=worker.REPORT_SCHEMA,
        report_validator=lambda value: worker.validate_report(
            value,
            task_mode="implementation",
        ),
        report_timeout=10,
    )

    prompts = [json.loads(line) for line in prompt_log.read_text().splitlines()]
    invariant = (
        "If status is completed, every listed test outcome MUST be passed or failed; "
        "omit optional or out-of-scope checks that were not run. If a required check "
        "could not run, status MUST be blocked with block_kind dependency. Never mark "
        "an unrun check as passed."
    )
    assert result.report["tests"][0]["outcome"] == "passed"
    assert validation_errors == [
        None,
        "A completed worker report cannot contain not_run tests",
    ]
    assert all(invariant in prompt for prompt in report_prompts)
    assert len(prompts) == 3
    assert {item["pid"] for item in prompts} == {prompts[0]["pid"]}
    assert {item["session_id"] for item in prompts} == {result.session_id}
    assert [item["has_output_schema"] for item in prompts] == [False, True, True]


def test_acp_second_completed_not_run_blocks_with_safe_evidence(tmp_path):
    hermes = FakeHermes(tmp_path)
    parsed = args(tmp_path, transport="acp")
    parsed.task_mode = "review"
    prompt_log = tmp_path.parent / f"{tmp_path.name}-second-not-run-prompt-log.ndjson"

    def acp_runner(_argv, **kwargs):
        return worker.run_grok_acp(
            [
                sys.executable,
                str(FAKE_ACP_AGENT),
                "--structured-report",
                "--invalid-completed-not-run-always",
                "--prompt-log",
                str(prompt_log),
            ],
            **kwargs,
        )

    def no_headless(*_args, **_kwargs):
        raise AssertionError("ACP report failure must not use headless fallback")

    assert (
        worker.execute(
            parsed,
            command_runner=hermes,
            grok_runner=no_headless,
            acp_runner=acp_runner,
        )
        == 1
    )

    prompts = [json.loads(line) for line in prompt_log.read_text().splitlines()]
    terminal = terminal_commands(hermes)
    message = terminal[0][-1]
    assert len(terminal) == 1
    assert terminal[0][4:7] == ["block", "--kind", "transient"]
    assert "A completed worker report cannot contain not_run tests" in message
    assert '"status":"completed"' in message
    assert '"block_kind":null' in message
    assert '"block_reason_chars":0' in message
    assert '"block_reason_bytes":0' in message
    assert hashlib.sha256(b"").hexdigest() in message
    assert '"tests_count":1' in message
    assert '"test_outcomes":{"failed":0,"invalid":0,"not_run":1,"passed":0}' in message
    assert "pytest -q" not in message
    assert '"details"' not in message
    assert len(prompts) == 3
    assert {item["pid"] for item in prompts} == {prompts[0]["pid"]}
    assert len({item["session_id"] for item in prompts}) == 1
    assert [item["has_output_schema"] for item in prompts] == [False, True, True]


def test_acp_second_invalid_completed_block_reason_blocks_with_safe_evidence(tmp_path):
    hermes = FakeHermes(tmp_path)
    parsed = args(tmp_path, transport="acp")
    prompt_log = (
        tmp_path.parent / f"{tmp_path.name}-second-invalid-report-prompt-log.ndjson"
    )
    blocker = "Unexpected non-empty blocker text."

    def acp_runner(_argv, **kwargs):
        return worker.run_grok_acp(
            [
                sys.executable,
                str(FAKE_ACP_AGENT),
                "--structured-report",
                "--invalid-completed-block-reason-always",
                "--prompt-log",
                str(prompt_log),
            ],
            **kwargs,
        )

    def no_headless(*_args, **_kwargs):
        raise AssertionError("ACP report failure must not use headless fallback")

    assert (
        worker.execute(
            parsed,
            command_runner=hermes,
            grok_runner=no_headless,
            acp_runner=acp_runner,
        )
        == 1
    )

    prompts = [json.loads(line) for line in prompt_log.read_text().splitlines()]
    terminal = terminal_commands(hermes)
    message = terminal[0][-1]
    assert len(terminal) == 1
    assert terminal[0][4:7] == ["block", "--kind", "transient"]
    assert "A completed worker report must have an empty block_reason" in message
    assert blocker not in message
    assert '"status":"completed"' in message
    assert f'"block_reason_chars":{len(blocker)}' in message
    assert f'"block_reason_bytes":{len(blocker.encode())}' in message
    assert hashlib.sha256(blocker.encode()).hexdigest() in message
    assert len(prompts) == 3
    assert {item["pid"] for item in prompts} == {prompts[0]["pid"]}
    assert len({item["session_id"] for item in prompts}) == 1
    assert [item["has_output_schema"] for item in prompts] == [False, True, True]


def test_acp_nonterminal_work_stop_never_starts_the_report_phase(tmp_path):
    prompt_log = tmp_path / "nonterminal-prompt-log.ndjson"

    with pytest.raises(worker.AdapterError, match="terminal turn: max_tokens"):
        worker.run_grok_acp(
            [
                sys.executable,
                str(FAKE_ACP_AGENT),
                "--structured-report",
                "--work-stop-reason",
                "max_tokens",
                "--prompt-log",
                str(prompt_log),
            ],
            env={},
            cwd=tmp_path,
            prompt="Perform substantive work.",
            timeout=10,
            no_progress_timeout=0,
            progress_probe=lambda: False,
            report_prompt_factory=lambda stop_reason, error: "Return the report.",
            report_schema={"type": "object"},
            report_validator=lambda value: None,
            report_timeout=10,
        )

    prompts = [json.loads(line) for line in prompt_log.read_text().splitlines()]
    assert len(prompts) == 1
    assert prompts[0]["has_output_schema"] is False


def test_acp_protocol_soak_has_30_deterministic_terminal_results(tmp_path):
    observed_sessions = []
    observed_pids = []

    for index in range(30):
        session_id = f"{index:08d}-1234-4234-8234-123456789abc"
        pid_file = tmp_path / f"soak-{index}.pid"
        result = run_fake_acp(
            tmp_path,
            "--session-id",
            session_id,
            "--pid-file",
            str(pid_file),
        )
        observed_sessions.append(result.session_id)
        observed_pids.append(int(pid_file.read_text(encoding="utf-8")))
        assert result.stop_reason == "end_turn"
        assert result.update_count == 0

    assert observed_sessions == [
        f"{index:08d}-1234-4234-8234-123456789abc" for index in range(30)
    ]
    assert all(not process_exists(pid) for pid in observed_pids)


@pytest.mark.parametrize("mode", ["no-newline", "malformed"])
def test_acp_invalid_or_unterminated_frame_is_bounded_and_reaps_child(tmp_path, mode):
    pid_file = tmp_path / f"{mode}.pid"

    with pytest.raises(worker.GrokTimeout):
        run_fake_acp(
            tmp_path,
            "--frames",
            str(256 * 1024),
            "--mode",
            mode,
            "--pid-file",
            str(pid_file),
            timeout=0.1,
        )

    pid = int(pid_file.read_text())
    assert not process_exists(pid)


def test_acp_frame_above_50_mib_is_explicit_capability_failure(tmp_path):
    with pytest.raises(worker.GrokCapabilityError, match="bounded.*receive limit"):
        run_fake_acp(
            tmp_path,
            "--frames",
            str(worker.ACP_STREAM_LIMIT_BYTES + 2),
            timeout=15,
        )


def test_acp_timeout_during_large_chunked_frame_reaps_child(tmp_path):
    pid_file = tmp_path / "cancel.pid"

    with pytest.raises(worker.GrokTimeout):
        run_fake_acp(
            tmp_path,
            "--frames",
            str(8 * 1024 * 1024),
            "--chunk-size",
            str(64 * 1024),
            "--delay",
            "0.01",
            "--mode",
            "stall-after-prefix",
            "--pid-file",
            str(pid_file),
            timeout=0.1,
        )

    pid = int(pid_file.read_text())
    assert not process_exists(pid)


@pytest.mark.linux_only
@pytest.mark.live_system_guard_bypass
def test_acp_normal_exit_reaps_detached_tool_descendant(tmp_path):
    pid_file = tmp_path / "detached-tool.pid"

    result = run_fake_acp(
        tmp_path,
        "--detached-child-pid-file",
        str(pid_file),
    )

    pid = int(pid_file.read_text())
    try:
        assert result.stop_reason == "end_turn"
        assert not process_exists(pid)
    finally:
        if process_exists(pid):
            os.kill(pid, signal.SIGKILL)


def test_acp_reports_a_bounded_frame_overrun_as_capability(tmp_path, monkeypatch):
    @contextlib.asynccontextmanager
    async def spawn_agent_process(client, command, *argv, **kwargs):
        del client, command, argv, kwargs
        raise ValueError("Separator is found, but chunk is longer than limit")
        yield

    fake_acp = types.SimpleNamespace(
        PROTOCOL_VERSION=1,
        spawn_agent_process=spawn_agent_process,
    )
    monkeypatch.setitem(sys.modules, "acp", fake_acp)

    try:
        worker.run_grok_acp(
            ["grok", "agent", "stdio"],
            env={},
            cwd=tmp_path,
            prompt="Implement the card.",
            timeout=1,
            no_progress_timeout=0,
            progress_probe=lambda: False,
        )
    except worker.GrokCapabilityError as exc:
        assert "exceeded the bounded" in str(exc)
        assert "receive limit" in str(exc)
    else:
        raise AssertionError("expected GrokCapabilityError")


def test_acp_early_auth_exit_is_a_capability_failure(tmp_path, monkeypatch):
    class Connection:
        async def initialize(self, protocol_version):
            del protocol_version
            await asyncio.Event().wait()

    class Stderr:
        async def read(self, limit):
            del limit
            return b"Not signed in. Set XAI_API_KEY."

    class Process:
        returncode = 1
        stderr = Stderr()

        async def wait(self):
            return 1

    @contextlib.asynccontextmanager
    async def spawn_agent_process(client, command, *argv, **kwargs):
        del client, command, argv, kwargs
        yield Connection(), Process()

    fake_acp = types.SimpleNamespace(
        PROTOCOL_VERSION=1,
        spawn_agent_process=spawn_agent_process,
    )
    monkeypatch.setitem(sys.modules, "acp", fake_acp)

    try:
        worker.run_grok_acp(
            ["grok", "agent", "stdio"],
            env={},
            cwd=tmp_path,
            prompt="Implement the card.",
            timeout=1,
            no_progress_timeout=0,
            progress_probe=lambda: False,
        )
    except worker.GrokCapabilityError as exc:
        assert str(exc) == "Grok authentication capability is unavailable"
    else:
        raise AssertionError("expected GrokCapabilityError")


def test_acp_auth_required_rpc_is_a_capability_failure(tmp_path, monkeypatch):
    class AuthRequired(Exception):
        code = -32000

    class Connection:
        async def initialize(self, protocol_version):
            del protocol_version
            raise AuthRequired("Authentication required")

    @contextlib.asynccontextmanager
    async def spawn_agent_process(client, command, *argv, **kwargs):
        del client, command, argv, kwargs
        yield Connection(), types.SimpleNamespace(stderr=None)

    fake_acp = types.SimpleNamespace(
        PROTOCOL_VERSION=1,
        spawn_agent_process=spawn_agent_process,
    )
    monkeypatch.setitem(sys.modules, "acp", fake_acp)

    try:
        worker.run_grok_acp(
            ["grok", "agent", "stdio"],
            env={},
            cwd=tmp_path,
            prompt="Implement the card.",
            timeout=1,
            no_progress_timeout=0,
            progress_probe=lambda: False,
        )
    except worker.GrokCapabilityError as exc:
        assert str(exc) == "Grok authentication capability is unavailable"
    else:
        raise AssertionError("expected GrokCapabilityError")


def test_review_acp_is_created_and_reported_read_only(tmp_path):
    hermes = FakeHermes(tmp_path)
    parsed = args(tmp_path, transport="acp")
    parsed.task_mode = "review"

    def acp(
        argv,
        *,
        env,
        cwd,
        prompt,
        timeout,
        no_progress_timeout,
        progress_probe,
        report_prompt_factory,
        report_schema,
        report_validator,
        report_timeout,
    ):
        del argv, cwd, prompt, timeout, progress_probe
        assert env["GROK_SANDBOX"] == "read-only"
        assert no_progress_timeout == 0
        assert report_timeout == parsed.correction_timeout
        assert "capability" not in json.dumps(report_schema["properties"]["block_kind"])
        report_prompt = report_prompt_factory("end_turn", None)
        assert "read-only reporting phase" in report_prompt
        report_validator(review)
        return worker.AcpWorkResult(
            session_id="33333333-3333-4333-8333-333333333333",
            stop_reason="end_turn",
            update_count=5,
            tool_call_count=2,
            report=review,
        )

    review = report()
    review.update(
        summary="PASS: reviewed the pinned SHA without mutations.",
        changed_files=[],
        evidence=["Pinned SHA and focused checks were verified."],
    )

    def grok(*_args, **_kwargs):
        raise AssertionError("ACP review must not launch a headless report process")

    assert (
        worker.execute(
            parsed,
            command_runner=hermes,
            acp_runner=acp,
            grok_runner=grok,
        )
        == 0
    )


def test_nonterminal_acp_stop_is_blocked_before_report(tmp_path):
    hermes = FakeHermes(tmp_path)
    parsed = args(tmp_path, transport="acp")

    def acp(*_args, **_kwargs):
        return worker.AcpWorkResult(
            session_id="44444444-4444-4444-8444-444444444444",
            stop_reason="max_tokens",
            update_count=7,
            tool_call_count=1,
        )

    def grok(*_args, **_kwargs):
        raise AssertionError("a nonterminal ACP turn must not generate a final report")

    assert (
        worker.execute(
            parsed,
            command_runner=hermes,
            acp_runner=acp,
            grok_runner=grok,
        )
        == 1
    )
    terminal = terminal_commands(hermes)[0]
    assert terminal[4:7] == ["block", "--kind", "transient"]
    assert "ended without a terminal turn: max_tokens" in terminal[-1]


def test_grok_inspect_failure_is_bounded_and_does_not_forward_stderr(tmp_path):
    parsed = args(tmp_path)

    def inspect(argv, *, env, cwd=None, timeout=None):
        return subprocess.CompletedProcess(
            argv,
            1,
            "",
            "provider detail that must not enter the prompt or metadata",
        )

    summary = worker.inspect_grok_environment(parsed, {}, inspect)

    assert summary == {
        "status": "unavailable",
        "reason": "inspect_command_failed",
    }
    assert "provider detail" not in json.dumps(summary)


def test_project_context_capability_failure_blocks_before_grok(tmp_path, monkeypatch):
    hermes = FakeHermes(tmp_path)

    def unavailable(_workspace):
        raise worker.ProjectContextCapabilityError(
            "Hermes project-context capability is unavailable"
        )

    monkeypatch.setattr(worker, "build_project_context_pack", unavailable)

    def grok(*_args, **_kwargs):
        raise AssertionError("Grok must not run without its required context pack")

    assert worker.execute(args(tmp_path), command_runner=hermes, grok_runner=grok) == 1
    terminal = terminal_commands(hermes)[0]
    assert terminal[4:7] == ["block", "--kind", "capability"]
    assert terminal[-1] == "Hermes project-context capability is unavailable"


def test_project_context_preflight_cannot_mutate_workspace(tmp_path):
    hermes = FakeHermes(tmp_path)

    def inspect(argv, *, env, cwd=None, timeout=None):
        (cwd / "inspect-side-effect.txt").write_text("forbidden\n")
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    def grok(*_args, **_kwargs):
        raise AssertionError("Grok must not run after a preflight mutation")

    assert (
        worker.execute(
            args(tmp_path),
            command_runner=hermes,
            grok_runner=grok,
            inspect_runner=inspect,
        )
        == 1
    )
    terminal = terminal_commands(hermes)[0]
    assert terminal[4:7] == ["block", "--kind", "transient"]
    assert terminal[-1] == "Project-context preflight modified the workspace"


def test_read_only_review_allows_verified_zero_change_completion(tmp_path):
    hermes = FakeHermes(tmp_path)
    parsed = args(tmp_path)
    parsed.task_mode = "review"
    review = report()
    review["summary"] = "PASS: reviewed the pinned implementation SHA read-only."
    review["changed_files"] = []
    review["tests"] = [
        {
            "command": "git diff --exit-code",
            "outcome": "passed",
            "details": "No workspace changes were made.",
        }
    ]
    review["evidence"] = ["Reviewed pinned SHA abc123; no findings."]

    def grok(argv, *, env, cwd, timeout):
        rules = argv[argv.index("--rules") + 1]
        assert "read-only security reviewer" in rules
        return json.dumps({"result": review})

    assert worker.execute(parsed, command_runner=hermes, grok_runner=grok) == 0
    assert terminal_commands(hermes)[0][4] == "complete"
    metadata = json.loads(
        terminal_commands(hermes)[0][
            terminal_commands(hermes)[0].index("--metadata") + 1
        ]
    )
    assert metadata["task_mode"] == "review"
    assert metadata["changed_files"] == []


def test_implementation_completion_rejects_zero_changed_files():
    value = report()
    value["changed_files"] = []
    try:
        worker.validate_report(value, task_mode="implementation")
    except worker.AdapterError as exc:
        assert "changed file" in str(exc)
    else:
        raise AssertionError("expected AdapterError")


def test_exact_placeholder_completion_is_rejected():
    value = report()
    value.update(summary="placeholder", changed_files=[], tests=[], evidence=[])
    try:
        worker.validate_report(value, task_mode="implementation")
    except worker.AdapterError as exc:
        assert "placeholder" in str(exc)
    else:
        raise AssertionError("expected AdapterError")


def test_blocked_review_cannot_claim_pass():
    value = report("blocked")
    value["summary"] = "PASS: but evidence is unavailable."
    try:
        worker.validate_report(value, task_mode="review")
    except worker.AdapterError as exc:
        assert "cannot claim PASS" in str(exc)
    else:
        raise AssertionError("expected AdapterError")


def test_read_only_boundary_is_not_a_review_blocker():
    value = report("blocked")
    value["summary"] = "Stopped without reviewing."
    value["block_reason"] = "read-only review forbids edits"
    try:
        worker.validate_report(value, task_mode="review")
    except worker.AdapterError as exc:
        assert "not itself a blocker" in str(exc)
    else:
        raise AssertionError("expected AdapterError")


def test_completed_report_cannot_carry_block_reason():
    value = report()
    value["block_reason"] = "contradictory blocker"
    try:
        worker.validate_report(value, task_mode="implementation")
    except worker.AdapterError as exc:
        assert "empty block_reason" in str(exc)
    else:
        raise AssertionError("expected AdapterError")


def test_completed_report_uses_null_and_model_schema_excludes_capability():
    value = report()
    value["block_kind"] = None

    worker.validate_report(value, task_mode="implementation")

    block_kind_schema = worker.REPORT_SCHEMA["properties"]["block_kind"]
    assert "capability" not in json.dumps(block_kind_schema)
    assert {item.get("type") for item in block_kind_schema["anyOf"]} == {
        "null",
        "string",
    }


def test_parse_report_preserves_completed_null_block_kind():
    payload = report()

    parsed = worker.parse_report(json.dumps({"result": payload}))

    assert parsed["status"] == "completed"
    assert parsed["block_kind"] is None


def test_model_blocked_report_cannot_classify_a_capability_failure():
    value = report("blocked")
    value["block_kind"] = "capability"
    try:
        worker.validate_report(value, task_mode="implementation")
    except worker.AdapterError as exc:
        assert "adapter owns capability classification" in str(exc)
    else:
        raise AssertionError("expected AdapterError")


def test_blocked_report_requires_concrete_evidence():
    value = report("blocked")
    value["evidence"] = []
    try:
        worker.validate_report(value, task_mode="implementation")
    except worker.AdapterError as exc:
        assert "requires evidence" in str(exc)
    else:
        raise AssertionError("expected AdapterError")


def test_blocked_report_requires_a_test_record():
    value = report("blocked")
    value["tests"] = []
    try:
        worker.validate_report(value, task_mode="implementation")
    except worker.AdapterError as exc:
        assert "requires a test record" in str(exc)
    else:
        raise AssertionError("expected AdapterError")


def test_test_records_cannot_merge_checks_with_shell_operators():
    value = report("blocked")
    value["tests"][0]["command"] = "pytest -q && ruff check ."
    try:
        worker.validate_report(value, task_mode="implementation")
    except worker.AdapterError as exc:
        assert "one command at a time" in str(exc)
    else:
        raise AssertionError("expected AdapterError")


def test_read_only_review_that_mutates_workspace_is_blocked(tmp_path):
    hermes = FakeHermes(tmp_path)
    parsed = args(tmp_path)
    parsed.task_mode = "review"
    review = report()
    review.update(
        summary="PASS: claimed success despite a mutation.",
        changed_files=[],
        evidence=["review evidence"],
    )

    calls = 0

    def grok(argv, *, env, cwd, timeout):
        nonlocal calls
        calls += 1
        # The path was already dirty before review. Comparing only the set of
        # dirty paths would miss this content mutation.
        (cwd / "src" / "example.py").write_text("forbidden review mutation\n")
        return json.dumps({"result": review})

    assert worker.execute(parsed, command_runner=hermes, grok_runner=grok) == 1
    assert calls == 1
    assert terminal_commands(hermes)[0][4:7] == ["block", "--kind", "transient"]


def test_blocked_report_always_blocks_card(tmp_path):
    hermes = FakeHermes(tmp_path)

    def grok(argv, *, env, cwd, timeout):
        return json.dumps(report("blocked"))

    assert worker.execute(args(tmp_path), command_runner=hermes, grok_runner=grok) == 0
    terminals = terminal_commands(hermes)
    assert len(terminals) == 1
    assert terminals[0][4:7] == ["block", "--kind", "dependency"]
    assert "Need an unavailable dependency." in terminals[0]


def test_invalid_first_output_gets_one_same_session_correction(tmp_path):
    hermes = FakeHermes(tmp_path)
    grok_calls = []

    def grok(argv, *, env, cwd, timeout):
        grok_calls.append(list(argv))
        if len(grok_calls) == 1:
            (cwd / "src" / "example.py").write_text("# repaired in initial phase\n")
            return json.dumps({"result": "finished as prose"})
        return json.dumps({"result": report()})

    assert worker.execute(args(tmp_path), command_runner=hermes, grok_runner=grok) == 0
    assert len(grok_calls) == 2
    session_id = grok_calls[0][grok_calls[0].index("--session-id") + 1]
    assert grok_calls[1][grok_calls[1].index("--resume") + 1] == session_id
    assert "--session-id" not in grok_calls[1]
    assert grok_calls[1][grok_calls[1].index("--rules") + 1] == worker.WORKER_RULES
    assert terminal_commands(hermes)[0][4] == "complete"


def test_empty_changed_files_is_corrected_with_observed_run_delta(tmp_path):
    hermes = FakeHermes(tmp_path)
    grok_calls = []
    invalid = report()
    invalid["changed_files"] = []

    def grok(argv, *, env, cwd, timeout):
        grok_calls.append(list(argv))
        if len(grok_calls) == 1:
            (cwd / "src" / "example.py").write_text("# actual Grok change\n")
            return json.dumps({"result": invalid})
        correction = argv[argv.index("-p") + 1]
        assert "requires at least one changed file" in correction
        assert (
            'candidate paths change during this invocation: ["src/example.py"]'
            in correction
        )
        assert "evidence, not authorization" in correction
        return json.dumps({"result": report()})

    assert worker.execute(args(tmp_path), command_runner=hermes, grok_runner=grok) == 0
    assert len(grok_calls) == 2
    terminal = terminal_commands(hermes)[0]
    assert terminal[4] == "complete"
    metadata = json.loads(terminal[terminal.index("--metadata") + 1])
    assert metadata["changed_files"] == ["src/example.py"]
    assert metadata["observed_run_changes"] == ["src/example.py"]


def test_mismatched_changed_files_gets_one_observed_delta_correction(tmp_path):
    hermes = FakeHermes(tmp_path)
    grok_calls = []
    mismatched = report()
    mismatched["changed_files"] = ["src/not-actually-changed.py"]

    def grok(argv, *, env, cwd, timeout):
        grok_calls.append(list(argv))
        if len(grok_calls) == 1:
            (cwd / "src" / "example.py").write_text("# actual Grok change\n")
            return json.dumps({"result": mismatched})
        correction = argv[argv.index("-p") + 1]
        assert "do not match changes made during this adapter run" in correction
        assert '["src/example.py"]' in correction
        return json.dumps({"result": report()})

    assert worker.execute(args(tmp_path), command_runner=hermes, grok_runner=grok) == 0
    assert len(grok_calls) == 2
    assert terminal_commands(hermes)[0][4] == "complete"


def test_preexisting_dirty_diff_does_not_count_as_a_run_change(tmp_path, capsys):
    hermes = FakeHermes(tmp_path)
    calls = 0

    def grok(argv, *, env, cwd, timeout):
        nonlocal calls
        calls += 1
        return json.dumps({"result": report()})

    assert worker.execute(args(tmp_path), command_runner=hermes, grok_runner=grok) == 1
    assert calls == 2
    terminal = terminal_commands(hermes)[0]
    assert terminal[4:7] == ["block", "--kind", "transient"]
    assert terminal[-1] == (
        "A completed implementation has no workspace changes from this adapter run"
    )
    failure = json.loads(capsys.readouterr().out)
    assert failure["status"] == "blocked"
    assert failure["adapter_execution"]["classification"] == "no_workspace_change"
    assert failure["adapter_execution"]["terminal_action"] == "block"
    assert failure["adapter_execution"]["phase"] == "work_report"
    assert failure["adapter_execution"]["run_id"]


def test_workspace_delta_detects_change_to_preexisting_dirty_file(tmp_path):
    args(tmp_path)
    before = worker.workspace_snapshot(tmp_path)

    (tmp_path / "src" / "example.py").write_text("# second dirty state\n")
    after = worker.workspace_snapshot(tmp_path)

    assert set(before) == {"src/example.py"}
    assert set(after) == {"src/example.py"}
    assert worker.workspace_delta(before, after) == {"src/example.py"}


def test_terminal_report_schema_excludes_progress_state():
    assert worker.REPORT_SCHEMA["properties"]["status"]["enum"] == [
        "completed",
        "blocked",
    ]
    value = report("working")
    value.update(
        summary="Reading the failing tests before editing.",
        changed_files=[],
        tests=[],
        evidence=[],
        block_reason="",
        block_kind="capability",
    )

    try:
        worker.validate_report(value)
    except worker.AdapterError as exc:
        assert "invalid status" in str(exc)
    else:
        raise AssertionError("expected AdapterError")


def test_correction_receives_exact_validation_failure(tmp_path):
    hermes = FakeHermes(tmp_path)
    grok_calls = []
    invalid = report("blocked")
    invalid.update(
        summary="Reading the Contract before editing.",
        changed_files=[],
        tests=[],
        evidence=[],
        block_reason="",
        block_kind="capability",
    )

    def grok(argv, *, env, cwd, timeout):
        grok_calls.append(list(argv))
        if len(grok_calls) == 1:
            return json.dumps({"result": invalid})
        correction = argv[argv.index("-p") + 1]
        assert "A blocked worker report requires block_reason" in correction
        assert (
            "Do not repeat repository inspection merely to repair report fields"
            in correction
        )
        return json.dumps({"result": report("blocked")})

    assert worker.execute(args(tmp_path), command_runner=hermes, grok_runner=grok) == 0
    assert len(grok_calls) == 2
    assert terminal_commands(hermes)[0][4:7] == ["block", "--kind", "dependency"]


def test_grok_command_pins_general_purpose_agent(tmp_path):
    parsed = args(tmp_path)
    command = worker._grok_command(parsed, "session-id", "prompt")

    assert command[command.index("--agent") + 1] == "general-purpose"


def test_headless_correction_does_not_redeclare_agent(tmp_path):
    parsed = args(tmp_path)

    correction = worker._resume_command(
        parsed,
        "session-id",
        worker.AdapterError("invalid report"),
    )

    assert "--resume" in correction
    assert "--agent" not in correction


def test_correction_timeout_names_the_failed_phase(tmp_path):
    hermes = FakeHermes(tmp_path)
    calls = 0

    def grok(argv, *, env, cwd, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "not-json"
        raise worker.GrokTimeout("Grok CLI timed out after 120 seconds")

    assert worker.execute(args(tmp_path), command_runner=hermes, grok_runner=grok) == 1
    terminal = terminal_commands(hermes)[0]
    assert terminal[4:7] == ["block", "--kind", "transient"]
    assert terminal[-1] == "Grok CLI correction phase timed out after 120 seconds"


def test_prompt_scopes_repo_lifecycle_without_disabling_repo_quality_rules():
    prompt = worker._prompt("Title: Make the requested edit")

    assert "Editing files there and running tests are explicitly authorized" in prompt
    assert "The upstream Foreman owns repository lifecycle actions" in prompt
    assert "Leave the implementation as an uncommitted diff" in prompt
    assert "continue to obey all repository scope, quality, security" in prompt
    assert "not a reason to avoid editing or to block" in prompt


def test_timeout_is_recorded_as_transient_block(tmp_path):
    hermes = FakeHermes(tmp_path)

    def grok(argv, *, env, cwd, timeout):
        raise worker.GrokTimeout("Grok CLI timed out after 1 seconds")

    assert worker.execute(args(tmp_path), command_runner=hermes, grok_runner=grok) == 1
    terminals = terminal_commands(hermes)
    assert len(terminals) == 1
    assert terminals[0][4:7] == ["block", "--kind", "transient"]
    assert terminals[0][-1] == "Grok CLI initial phase timed out after 900 seconds"


def test_grok_auth_failure_is_recorded_as_capability_block(tmp_path):
    hermes = FakeHermes(tmp_path)

    def grok(argv, *, env, cwd, timeout):
        raise worker.GrokCapabilityError(
            "Grok authentication capability is unavailable"
        )

    assert worker.execute(args(tmp_path), command_runner=hermes, grok_runner=grok) == 1
    terminals = terminal_commands(hermes)
    assert len(terminals) == 1
    assert terminals[0][4:7] == ["block", "--kind", "capability"]
    assert terminals[0][-1] == "Grok authentication capability is unavailable"


def test_grok_auth_exit_is_classified_without_forwarding_provider_details():
    error = worker.grok_exit_error(
        1,
        "",
        "Not signed in. Run grok login --device-code; supplied detail is private.",
    )

    assert isinstance(error, worker.GrokCapabilityError)
    assert str(error) == "Grok authentication capability is unavailable"
    assert "private" not in str(error)


def test_grok_code_2_exit_preserves_bounded_sanitized_diagnostics(tmp_path):
    secret = "xai-test-secret-value"
    stderr = (
        f"XAI_API_KEY={secret}\n"
        "Unicode diagnostic: 接続に失敗しました 🚫\n"
        + ("diagnostic-line-" * 600)
        + "\nAuthorization: Bearer another-secret-value\n"
        + "tail-marker"
    )
    cli = tmp_path / "fake-grok-code-2.py"
    cli.write_text(f"import sys\nsys.stderr.write({stderr!r})\nraise SystemExit(2)\n")

    try:
        worker.run_grok_acp(
            [sys.executable, str(cli)],
            env={},
            cwd=tmp_path,
            prompt="exercise code 2",
            timeout=5,
            no_progress_timeout=0,
            progress_probe=lambda: False,
            poll_interval=0.01,
        )
    except worker.AdapterError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected AdapterError")

    assert "Grok CLI exited with code 2" in message
    assert "phase=ACP work" in message
    assert secret not in message
    assert "another-secret-value" not in message
    assert "[REDACTED]" in message
    assert "接続に失敗しました" in message
    assert "tail-marker" in message
    assert '"truncated":true' in message
    assert f'"chars":{len(stderr)}' in message
    assert f'"bytes":{len(stderr.encode())}' in message
    assert hashlib.sha256(stderr.encode()).hexdigest() in message


def test_sanitized_stderr_preview_obeys_hard_character_limit():
    evidence = worker._stderr_evidence("diagnostic-line-" * 600)

    assert evidence["truncated"] is True
    assert len(evidence["preview"]) <= worker.STDERR_PREVIEW_CHARS


@pytest.mark.parametrize(
    ("exit_on_prompt", "invalid_first_report", "expected_phase"),
    [(2, False, "terminal report"), (3, True, "report correction")],
)
def test_acp_code_2_preserves_terminal_phase_and_safe_evidence(
    tmp_path, exit_on_prompt, invalid_first_report, expected_phase
):
    argv = [
        sys.executable,
        str(FAKE_ACP_AGENT),
        "--structured-report",
        "--mode",
        "exit-2",
        "--exit-on-prompt",
        str(exit_on_prompt),
    ]
    if invalid_first_report:
        argv.append("--invalid-first-report")

    with pytest.raises(worker.GrokProcessExit) as raised:
        worker.run_grok_acp(
            argv,
            env={},
            cwd=tmp_path,
            prompt="Perform substantive work.",
            timeout=10,
            no_progress_timeout=0,
            progress_probe=lambda: False,
            report_prompt_factory=lambda stop_reason, error: (
                "Correct the report." if error else f"Report after {stop_reason}."
            ),
            report_schema={"type": "object"},
            report_validator=require_structured_summary,
            report_timeout=10,
            poll_interval=0.005,
        )

    message = str(raised.value)
    assert f"phase={expected_phase}" in message
    assert "code 2" in message
    assert "super-secret-value" not in message
    assert "[REDACTED]" in message
    assert "接続に失敗しました" in message
    assert "tail-marker" in message


@pytest.mark.parametrize(
    ("stall_on_prompt", "invalid_first_report", "expected_phase"),
    [(2, False, "terminal report"), (3, True, "report correction")],
)
def test_acp_report_timeout_cancels_and_reaps_session(
    tmp_path, stall_on_prompt, invalid_first_report, expected_phase
):
    pid_file = tmp_path / "report-timeout.pid"
    cancel_log = tmp_path / "report-timeout-cancelled.txt"

    argv = [
        sys.executable,
        str(FAKE_ACP_AGENT),
        "--structured-report",
        "--stall-on-prompt",
        str(stall_on_prompt),
        "--pid-file",
        str(pid_file),
        "--cancel-log",
        str(cancel_log),
    ]
    if invalid_first_report:
        argv.append("--invalid-first-report")

    with pytest.raises(worker.GrokTimeout, match=f"{expected_phase}.*timed out"):
        worker.run_grok_acp(
            argv,
            env={},
            cwd=tmp_path,
            prompt="Perform substantive work.",
            timeout=10,
            no_progress_timeout=0,
            progress_probe=lambda: False,
            report_prompt_factory=lambda stop_reason, error: (
                "Correct the report." if error else f"Report after {stop_reason}."
            ),
            report_schema={"type": "object"},
            report_validator=require_structured_summary,
            report_timeout=0.1,
            poll_interval=0.005,
        )

    assert cancel_log.read_text() == "88888888-8888-4888-8888-888888888888"
    assert not process_exists(int(pid_file.read_text()))


def test_two_invalid_outputs_are_recorded_as_transient_block(tmp_path):
    hermes = FakeHermes(tmp_path)
    calls = 0

    def grok(argv, *, env, cwd, timeout):
        nonlocal calls
        calls += 1
        return "not-json"

    assert worker.execute(args(tmp_path), command_runner=hermes, grok_runner=grok) == 1
    assert calls == 2
    assert terminal_commands(hermes)[0][4:7] == ["block", "--kind", "transient"]


def test_child_environment_does_not_mutate_parent_mapping():
    source = {"RIGHTCODE_API_KEY": "secret"}
    child = worker.child_environment(source)
    assert source == {"RIGHTCODE_API_KEY": "secret"}
    assert child["RIGHTCODE_GROK_API_KEY"] == "secret"
    assert child["XAI_API_KEY"] == "secret"
    assert child["GROK_MODELS_BASE_URL"] == "https://rightapi.ai/grok/v1"
    assert child["HERMES_PROFILE"] == "worker-grok-cli"


def test_untracked_python_test_caches_are_not_workspace_changes(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Adapter Tests",
            "-c",
            "user.email=adapter@example.invalid",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "baseline",
        ],
        check=True,
    )
    (tmp_path / "probe.py").write_text("value = 1\n")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "probe.cpython-314.pyc").write_bytes(b"generated bytecode")
    pytest_cache = tmp_path / ".pytest_cache"
    pytest_cache.mkdir()
    (pytest_cache / "README.md").write_text("generated cache\n")

    assert worker.workspace_changes(tmp_path) == {"probe.py"}


def test_explicit_grok_auth_configuration_is_preserved():
    source = {
        "RIGHTCODE_API_KEY": "rightcode-secret",
        "RIGHTCODE_GROK_API_KEY": "explicit-grok-secret",
        "XAI_API_KEY": "explicit-xai-secret",
        "GROK_MODELS_BASE_URL": "https://grok-proxy.example/v1",
    }

    child = worker.child_environment(source)

    assert child["RIGHTCODE_GROK_API_KEY"] == "explicit-grok-secret"
    assert child["XAI_API_KEY"] == "explicit-xai-secret"
    assert child["GROK_MODELS_BASE_URL"] == "https://grok-proxy.example/v1"


def test_claim_workspace_parser_accepts_real_cli_shape(tmp_path):
    parsed = worker.parse_claimed_workspace(f"Claimed task-1\nWorkspace: {tmp_path}\n")
    assert parsed == tmp_path.resolve()


def test_readiness_diagnostic_names_failing_probe_and_redacts_secret(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "worktree"
    workspace.mkdir()

    def failed_probe(argv, **kwargs):
        label = (
            "diff" if "diff" in argv else "untracked" if "ls-files" in argv else "head"
        )
        stderr = "fatal: permission denied API_KEY=must-not-leak"
        return subprocess.CompletedProcess(argv, 128, b"", stderr.encode())

    monkeypatch.setattr(worker.subprocess, "run", failed_probe)
    with pytest.raises(worker.WorkspaceReadinessError) as raised:
        worker.wait_for_claimed_workspace(workspace, timeout=0)

    message = str(raised.value)
    assert "probe=diff" in message
    assert "returncode=128" in message
    assert "permission denied" in message
    assert "must-not-leak" not in message


@pytest.mark.parametrize(
    "stderr",
    [
        "API error (status 500 Internal Server Error)",
        "API error status=500",
        "upstream status_code=503",
        "HTTP 500",
        "No ResponseCompleted or ResponseIncomplete event received from Responses API",
    ],
)
def test_provider_failures_are_retryable_and_sanitized(stderr):
    secret = "xai-secret-provider-value"
    error = worker.grok_exit_error(1, "", f"{stderr}\nXAI_API_KEY={secret}")

    assert isinstance(error, worker.GrokProviderError)
    assert error.retryable is True
    assert stderr in str(error)
    assert secret not in str(error)


@pytest.mark.parametrize(
    "diagnostic",
    ["HTTP 401 Unauthorized", "provider status=401", "provider status_code=401"],
)
def test_common_401_forms_are_non_retryable_auth_capability_errors(diagnostic):
    error = worker.grok_exit_error(1, diagnostic, "")

    assert isinstance(error, worker.GrokCapabilityError)
    assert str(error) == "Grok authentication capability is unavailable"


def test_provider_stdout_diagnostics_are_retained_and_sanitized():
    secret = "stdout-provider-secret"
    error = worker.grok_exit_error(
        1,
        f"HTTP 503 upstream unavailable XAI_API_KEY={secret}",
        "",
    )

    assert isinstance(error, worker.GrokProviderError)
    assert "upstream unavailable" in str(error)
    assert secret not in str(error)


def test_alternate_provider_route_retries_only_before_workspace_mutation(tmp_path):
    args(tmp_path)
    pristine = worker.workspace_snapshot(tmp_path)
    routes = worker.provider_route_environments(
        {
            "PRIMARY_URL": "https://primary.invalid/v1",
            "PRIMARY_KEY": "primary-secret",
            "SECONDARY_URL": "https://secondary.invalid/v1",
            "SECONDARY_KEY": "secondary-secret",
        },
        [
            json.dumps({
                "name": "primary",
                "endpoint_env": "PRIMARY_URL",
                "key_env": "PRIMARY_KEY",
            }),
            json.dumps({
                "name": "secondary",
                "endpoint_env": "SECONDARY_URL",
                "key_env": "SECONDARY_KEY",
            }),
        ],
    )
    attempts = []

    def operation(env):
        attempts.append(env["GROK_MODELS_BASE_URL"])
        if len(attempts) == 1:
            raise worker.GrokProviderError(worker._stderr_evidence("status 503"))
        return "ok"

    result, selected = worker.run_with_provider_failover(
        operation,
        routes,
        workspace=tmp_path,
        pristine_snapshot=pristine,
    )
    assert (result, selected) == ("ok", "secondary")
    assert attempts == ["https://primary.invalid/v1", "https://secondary.invalid/v1"]

    def mutating_failure(env):
        (tmp_path / "src" / "example.py").write_text("partial work\n", encoding="utf-8")
        raise worker.GrokProviderError(worker._stderr_evidence("status 500"))

    with pytest.raises(
        worker.AdapterError, match="partial work was preserved"
    ) as raised:
        worker.run_with_provider_failover(
            mutating_failure,
            routes,
            workspace=tmp_path,
            pristine_snapshot=pristine,
        )
    message = str(raised.value)
    assert "stderr_evidence=" in message
    assert "status 500" in message


def test_headless_correction_reuses_selected_backup_route_environment(
    tmp_path, monkeypatch, capsys
):
    primary_endpoint = "https://primary.invalid/v1"
    secondary_endpoint = "https://secondary.invalid/v1"
    primary_key = "primary-credential-sentinel"
    secondary_key = "secondary-credential-sentinel"
    monkeypatch.setenv("PRIMARY_URL", primary_endpoint)
    monkeypatch.setenv("PRIMARY_KEY", primary_key)
    monkeypatch.setenv("SECONDARY_URL", secondary_endpoint)
    monkeypatch.setenv("SECONDARY_KEY", secondary_key)
    parsed = args(tmp_path)
    parsed.provider_route = [
        json.dumps({
            "name": "primary",
            "endpoint_env": "PRIMARY_URL",
            "key_env": "PRIMARY_KEY",
        }),
        json.dumps({
            "name": "secondary",
            "endpoint_env": "SECONDARY_URL",
            "key_env": "SECONDARY_KEY",
        }),
    ]
    parsed.claim_ttl = 3000
    hermes = FakeHermes(tmp_path)
    route_observations = []

    def grok(argv, *, env, cwd, timeout):
        del timeout
        route_observations.append((
            env.get("GROK_MODELS_BASE_URL") == secondary_endpoint,
            env.get("XAI_API_KEY") == secondary_key,
            env.get("RIGHTCODE_GROK_API_KEY") == secondary_key,
        ))
        if env["GROK_MODELS_BASE_URL"] == primary_endpoint:
            raise worker.GrokProviderError(worker._stderr_evidence("status 503"))
        if "--resume" not in argv:
            (cwd / "src" / "example.py").write_text(
                "# changed on selected backup route\n", encoding="utf-8"
            )
            return json.dumps({"result": "invalid report"})
        return json.dumps({"result": report()})

    assert worker.execute(parsed, command_runner=hermes, grok_runner=grok) == 0
    assert route_observations == [
        (False, False, False),
        (True, True, True),
        (True, True, True),
    ]
    output = capsys.readouterr()
    assert primary_key not in output.out + output.err
    assert secondary_key not in output.out + output.err


def test_structured_acceptance_contract_rejects_missing_and_shell_operators(tmp_path):
    with pytest.raises(worker.AcceptanceContractError, match="required"):
        worker.parse_acceptance_commands([])

    malformed = json.dumps({
        "label": "checks",
        "argv": ["python", "-c", "pass", "&&", "ruff", "check", "."],
        "timeout": 30,
    })
    with pytest.raises(worker.AcceptanceContractError, match="shell operators"):
        worker.parse_acceptance_commands([malformed])


def test_structured_acceptance_commands_keep_independent_labels_and_timeouts(tmp_path):
    args(tmp_path)
    commands = worker.parse_acceptance_commands([
        json.dumps({
            "label": "unit",
            "argv": [sys.executable, "-c", "raise SystemExit(0)"],
            "timeout": 2,
        }),
        json.dumps({
            "label": "lint",
            "argv": [sys.executable, "-c", "raise SystemExit(7)"],
            "timeout": 3,
        }),
    ])

    result = worker.run_adapter_verification(commands, env={}, cwd=tmp_path, timeout=10)

    assert result["status"] == "failed"
    assert [(item["label"], item["returncode"]) for item in result["commands"]] == [
        ("unit", 0),
        ("lint", 7),
    ]
    assert [item["configured_timeout_seconds"] for item in result["commands"]] == [2, 3]


def test_identical_structured_acceptance_descriptors_execute_independently(tmp_path):
    args(tmp_path)
    marker = tmp_path.parent / f"{tmp_path.name}-acceptance-runs.txt"
    descriptor = json.dumps({
        "label": "duplicate",
        "argv": [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                f"Path({str(marker)!r}).open('a', encoding='utf-8').write('x')"
            ),
        ],
        "timeout": 2,
    })
    commands = worker.parse_acceptance_commands([descriptor, descriptor])

    result = worker.run_adapter_verification(commands, env={}, cwd=tmp_path, timeout=10)

    assert result["status"] == "passed"
    assert result["command_count"] == 2
    assert [item["label"] for item in result["commands"]] == [
        "duplicate",
        "duplicate",
    ]
    assert marker.read_text(encoding="utf-8") == "xx"


def test_verification_rejects_symlink_that_escapes_isolated_copy(tmp_path):
    args(tmp_path)
    external = tmp_path.parent / f"{tmp_path.name}-external.txt"
    external.write_text("untouched\n", encoding="utf-8")
    (tmp_path / "escape.txt").symlink_to(external)
    commands = worker.parse_acceptance_commands([
        json.dumps({
            "label": "escape",
            "argv": [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('escape.txt').write_text('mutated')",
            ],
            "timeout": 2,
        })
    ])

    with pytest.raises(worker.AdapterError, match="unsafe symlink"):
        worker.run_adapter_verification(commands, env={}, cwd=tmp_path, timeout=10)

    assert external.read_text(encoding="utf-8") == "untouched\n"


def test_claim_waits_for_materialized_git_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "materializing-worktree"
    workspace.mkdir()
    parsed = worker.build_parser().parse_args([
        "task-1",
        "--board",
        "adapter-test",
        "--workspace",
        str(workspace),
        "--transport",
        "headless",
        "--allow-experimental-headless",
        "--reviewer",
        "",
        "--command-timeout",
        "1",
        "--acceptance-command",
        json.dumps({
            "label": "materialized",
            "argv": [sys.executable, "-c", "raise SystemExit(0)"],
            "timeout": 1,
        }),
    ])
    hermes = FakeHermes(workspace)
    monkeypatch.setattr(
        worker,
        "build_project_context_pack",
        lambda _workspace: {
            "workspace_snapshot": "fixture",
            "root": str(workspace),
            "manifests": [],
            "verify_commands": [],
            "context_files": [],
        },
    )

    def materialize() -> None:
        threading.Event().wait(0.1)
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "-c",
                "user.name=Adapter Tests",
                "-c",
                "user.email=adapter@example.invalid",
                "commit",
                "--allow-empty",
                "-q",
                "-m",
                "baseline",
            ],
            check=True,
        )
        (workspace / "src").mkdir()
        (workspace / "src" / "example.py").write_text(
            "# materialized\n", encoding="utf-8"
        )

    materializer = threading.Thread(target=materialize)
    materializer.start()

    def grok(_argv, *, env, cwd, timeout):
        del env, timeout
        (cwd / "src" / "example.py").write_text(
            "# changed after claim readiness\n", encoding="utf-8"
        )
        return json.dumps({"result": report()})

    try:
        result = worker.execute(parsed, command_runner=hermes, grok_runner=grok)
    finally:
        materializer.join(timeout=5)

    assert result == 0
    assert terminal_commands(hermes)[0][4] == "complete"


def test_claim_waits_for_stable_workspace_snapshot(tmp_path, monkeypatch):
    parsed = args(tmp_path)
    hermes = FakeHermes(tmp_path)

    def finish_materialization() -> None:
        threading.Event().wait(0.12)
        (tmp_path / "materialized-late.txt").write_text("late file\n", encoding="utf-8")

    materializer = threading.Thread(target=finish_materialization)
    materializer.start()

    def project_context(_workspace):
        threading.Event().wait(0.18)
        return {
            "workspace_snapshot": "fixture",
            "root": str(tmp_path),
            "manifests": [],
            "verify_commands": [],
            "context_files": [],
        }

    monkeypatch.setattr(worker, "build_project_context_pack", project_context)

    def grok(_argv, *, env, cwd, timeout):
        del env, timeout
        (cwd / "src" / "example.py").write_text(
            "# changed after stable claim\n", encoding="utf-8"
        )
        return json.dumps({"result": report()})

    try:
        result = worker.execute(parsed, command_runner=hermes, grok_runner=grok)
    finally:
        materializer.join(timeout=5)

    assert result == 0
    assert terminal_commands(hermes)[0][4] == "complete"


def test_report_parser_rejects_unknown_fields():
    value = report()
    value["unexpected"] = True
    try:
        worker.parse_report(json.dumps(value))
    except worker.AdapterError as exc:
        assert "fields" in str(exc)
    else:
        raise AssertionError("expected AdapterError")


def test_report_parser_finds_json_text_inside_content_array():
    envelope = {
        "response": {"content": [{"type": "text", "text": json.dumps(report())}]}
    }
    assert worker.parse_report(json.dumps(envelope))["status"] == "completed"


def test_non_string_enum_is_safely_blocked_after_claim(tmp_path):
    hermes = FakeHermes(tmp_path)
    malformed = report()
    malformed["block_kind"] = []

    def grok(argv, *, env, cwd, timeout):
        return json.dumps({"structured_output": malformed})

    assert worker.execute(args(tmp_path), command_runner=hermes, grok_runner=grok) == 1
    assert terminal_commands(hermes)[0][4:7] == ["block", "--kind", "transient"]


def test_wrong_assignee_is_rejected_before_claim(tmp_path):
    def command(argv, *, env, cwd=None, timeout=None):
        if argv[4] == "show":
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"assignee": "foreman-long"}), ""
            )
        raise AssertionError("adapter must not claim a wrongly assigned task")

    def grok(argv, *, env, cwd, timeout):
        raise AssertionError("Grok must not run for a wrongly assigned task")

    assert worker.execute(args(tmp_path), command_runner=command, grok_runner=grok) == 1


def test_short_claim_ttl_is_rejected_before_kanban_or_grok(tmp_path):
    parsed = args(tmp_path)
    parsed.claim_ttl = 1

    def unexpected(*_args, **_kwargs):
        raise AssertionError("no subprocess should run for an unsafe claim TTL")

    assert (
        worker.execute(parsed, command_runner=unexpected, grok_runner=unexpected) == 1
    )


def test_missing_grok_binary_is_recorded_as_transient_block(tmp_path):
    hermes = FakeHermes(tmp_path)

    def missing(*_args, **_kwargs):
        raise FileNotFoundError("grok")

    assert (
        worker.execute(args(tmp_path), command_runner=hermes, grok_runner=missing) == 1
    )
    terminals = terminal_commands(hermes)
    assert terminals[0][4:7] == ["block", "--kind", "transient"]
    assert terminals[0][-1] == "Subprocess launch failed: FileNotFoundError"


def test_supplied_workspace_must_equal_claimed_workspace(tmp_path):
    claimed = tmp_path / "claimed"
    supplied = tmp_path / "supplied"
    claimed.mkdir()
    supplied.mkdir()
    hermes = FakeHermes(claimed)
    parsed = args(supplied)

    def grok(*_args, **_kwargs):
        raise AssertionError("Grok must not run outside the claimed workspace")

    assert worker.execute(parsed, command_runner=hermes, grok_runner=grok) == 1
    assert terminal_commands(hermes)[0][4:7] == ["block", "--kind", "transient"]
