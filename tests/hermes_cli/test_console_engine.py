from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from hermes_cli.console_engine import (
    ConsoleResult,
    HermesConsoleEngine,
    run_console_repl,
)


EXPECTED_CONSOLE_COMMANDS = {
    ("status",),
    ("doctor",),
    ("logs",),
    ("version",),
    ("dump",),
    ("debug", "share"),
    ("debug", "delete"),
    ("prompt-size",),
    ("insights",),
    ("security", "audit"),
    ("portal", "info"),
    ("portal", "tools"),
    ("backup",),
    ("import",),
    ("send",),
    ("config", "show"),
    ("config", "path"),
    ("config", "env-path"),
    ("config", "check"),
    ("config", "migrate"),
    ("config", "set"),
    ("sessions", "list"),
    ("sessions", "stats"),
    ("sessions", "export"),
    ("sessions", "rename"),
    ("sessions", "optimize"),
    ("sessions", "repair"),
    ("cron", "list"),
    ("cron", "status"),
    ("cron", "create"),
    ("cron", "edit"),
    ("cron", "pause"),
    ("cron", "resume"),
    ("cron", "run"),
    ("cron", "remove"),
    ("cron", "tick"),
    ("profile",),
    ("profile", "list"),
    ("profile", "show"),
    ("profile", "info"),
    ("profile", "create"),
    ("profile", "use"),
    ("profile", "describe"),
    ("profile", "rename"),
    ("profile", "delete"),
    ("profile", "export"),
    ("profile", "import"),
    ("profile", "install"),
    ("profile", "update"),
    ("tools", "list"),
    ("tools", "enable"),
    ("tools", "disable"),
    ("tools", "post-setup"),
    ("plugins", "list"),
    ("plugins", "enable"),
    ("plugins", "disable"),
    ("plugins", "install"),
    ("plugins", "update"),
    ("plugins", "remove"),
    ("skills", "browse"),
    ("skills", "search"),
    ("skills", "inspect"),
    ("skills", "list"),
    ("skills", "check"),
    ("skills", "list-modified"),
    ("skills", "diff"),
    ("skills", "install"),
    ("skills", "update"),
    ("skills", "audit"),
    ("skills", "uninstall"),
    ("skills", "reset"),
    ("skills", "opt-in"),
    ("skills", "opt-out"),
    ("skills", "repair-official"),
    ("skills", "snapshot", "export"),
    ("skills", "snapshot", "import"),
    ("skills", "tap", "list"),
    ("skills", "tap", "add"),
    ("skills", "tap", "remove"),
    ("mcp", "list"),
    ("mcp", "catalog"),
    ("mcp", "test"),
    ("mcp", "add"),
    ("mcp", "remove"),
    ("mcp", "install"),
    ("mcp", "login"),
    ("mcp", "reauth"),
    ("mcp", "configure"),
    ("mcp", "picker"),
    ("memory", "status"),
    ("memory", "off"),
    ("memory", "reset"),
    ("auth", "list"),
    ("auth", "status"),
    ("auth", "reset"),
    ("auth", "add"),
    ("auth", "remove"),
    ("auth", "logout"),
    ("auth", "spotify", "status"),
    ("auth", "spotify", "login"),
    ("auth", "spotify", "logout"),
    ("pairing", "list"),
    ("pairing", "approve"),
    ("pairing", "revoke"),
    ("pairing", "clear-pending"),
    ("webhook", "list"),
    ("webhook", "subscribe"),
    ("webhook", "remove"),
    ("webhook", "test"),
    ("hooks", "list"),
    ("hooks", "test"),
    ("hooks", "doctor"),
    ("hooks", "revoke"),
    ("slack", "manifest"),
    ("project", "list"),
    ("project", "show"),
    ("project", "create"),
    ("project", "add-folder"),
    ("project", "remove-folder"),
    ("project", "rename"),
    ("project", "set-primary"),
    ("project", "use"),
    ("project", "archive"),
    ("project", "restore"),
    ("project", "bind-board"),
    ("kanban", "init"),
    ("kanban", "boards", "list"),
    ("kanban", "boards", "create"),
    ("kanban", "boards", "rm"),
    ("kanban", "boards", "switch"),
    ("kanban", "boards", "current"),
    ("kanban", "boards", "rename"),
    ("kanban", "boards", "set-workdir"),
    ("kanban", "create"),
    ("kanban", "list"),
    ("kanban", "show"),
    ("kanban", "assign"),
    ("kanban", "reclaim"),
    ("kanban", "reassign"),
    ("kanban", "diagnose"),
    ("kanban", "link"),
    ("kanban", "unlink"),
    ("kanban", "claim"),
    ("kanban", "comment"),
    ("kanban", "complete"),
    ("kanban", "edit"),
    ("kanban", "block"),
    ("kanban", "schedule"),
    ("kanban", "unblock"),
    ("kanban", "promote"),
    ("kanban", "archive"),
    ("kanban", "stats"),
    ("kanban", "runs"),
    ("kanban", "heartbeat"),
    ("kanban", "assignments"),
    ("kanban", "context"),
    ("bundles", "list"),
    ("bundles", "show"),
    ("bundles", "create"),
    ("bundles", "delete"),
    ("bundles", "reload"),
    ("checkpoints", "status"),
    ("checkpoints", "list"),
    ("checkpoints", "prune"),
    ("checkpoints", "clear"),
    ("checkpoints", "clear-legacy"),
    ("curator", "status"),
    ("curator", "run"),
    ("curator", "pause"),
    ("curator", "resume"),
    ("curator", "pin"),
    ("curator", "unpin"),
    ("curator", "restore"),
    ("curator", "list-archived"),
    ("curator", "archive"),
    ("curator", "prune"),
    ("curator", "backup"),
    ("curator", "rollback"),
    ("pets", "list"),
    ("pets", "install"),
    ("pets", "select"),
    ("pets", "show"),
    ("pets", "off"),
    ("pets", "scale"),
    ("pets", "remove"),
    ("pets", "doctor"),
}


MUTATING_CONFIRMATION_SMOKE_COMMANDS = [
    "config set console.test true",
    "config migrate",
    "sessions rename abc123 new title",
    "sessions optimize",
    "cron create 'every 1h' 'say hello'",
    "cron remove abc123",
    "profile create tester --no-alias --no-skills",
    "profile delete tester",
    "tools disable web",
    "plugins install owner/repo --no-enable",
    "skills install openai/skills/example",
    "mcp add demo --url https://example.com/sse",
    "mcp configure github",
    "mcp picker",
    "backup --quick -o /tmp/hermes-console-test.zip",
    "import /tmp/hermes-console-test.zip",
    "send --to telegram hello",
    "memory reset --target memory",
    "auth remove openrouter 1",
    "pairing approve abc123",
    "webhook subscribe test --prompt hello",
    "hooks test pre_tool_call",
    "project create demo",
    "kanban create 'demo task'",
    "bundles create demo --skill skill-a",
    "checkpoints prune",
    "curator pause",
    "pets install cat",
]


def test_console_parses_bare_and_hermes_prefixed_commands(_isolate_hermes_home):
    engine = HermesConsoleEngine()

    bare = engine.execute("config path")
    prefixed = engine.execute("hermes config path")

    assert bare.status == "ok"
    assert prefixed.status == "ok"
    assert bare.output == prefixed.output
    assert bare.output.endswith("config.yaml")


def test_console_status_hides_cli_next_step_footer(
    monkeypatch: pytest.MonkeyPatch,
    _isolate_hermes_home,
):
    import hermes_cli.status as status_mod

    def fake_show_status(_args):
        print("◆ Sessions")
        print("Active: 3 session(s)")
        print()
        rule = "\u2500" * 60
        print(f"\x1b[2m{rule}\x1b[0m")
        print("\x1b[2m  Run 'hermes doctor' for detailed diagnostics\x1b[0m")
        print("\x1b[2m  Run 'hermes setup' to configure\x1b[0m")
        print()

    monkeypatch.setattr(status_mod, "show_status", fake_show_status)

    result = HermesConsoleEngine().execute("status")

    assert result.status == "ok"
    assert "Sessions" in result.output
    assert "Active: 3 session(s)" in result.output
    assert "hermes doctor" not in result.output
    assert "hermes setup" not in result.output
    assert "\u2500" not in result.output


def test_console_status_hides_osc_linked_cli_next_step_footer(
    monkeypatch: pytest.MonkeyPatch,
    _isolate_hermes_home,
):
    import hermes_cli.status as status_mod

    def osc_link(text: str) -> str:
        return f"\x1b]8;;https://example.test\x1b\\{text}\x1b]8;;\x1b\\"

    def fake_show_status(_args):
        print("◆ Sessions")
        print("Active: 3 session(s)")
        print()
        print(osc_link("\u2500" * 60))
        print(osc_link("  Run 'hermes doctor' for detailed diagnostics"))
        print(osc_link("  Run 'hermes setup' to configure"))
        print()

    monkeypatch.setattr(status_mod, "show_status", fake_show_status)

    result = HermesConsoleEngine().execute("status")

    assert result.status == "ok"
    assert "Sessions" in result.output
    assert "Active: 3 session(s)" in result.output
    assert "hermes doctor" not in result.output
    assert "hermes setup" not in result.output
    assert "https://example.test" not in result.output
    assert "\u2500" not in result.output


def test_console_help_uses_cli_subcommand_summaries():
    help_text = HermesConsoleEngine().help_text()

    assert "skills list" in help_text
    assert "List installed skills" in help_text
    assert "Show all tools and their enabled/disabled status" in help_text
    assert "Remove an MCP server" in help_text
    assert "Check pet setup + terminal graphics support" in help_text
    assert "Run `hermes skills list`" not in help_text
    assert "Run `hermes tools list`" not in help_text


def test_console_help_table_keeps_long_summaries_compact():
    help_text = HermesConsoleEngine().help_text()

    slack_line = next(
        line for line in help_text.splitlines() if line.strip().startswith("slack manifest")
    )

    assert len(slack_line) <= 112
    assert slack_line.endswith("...")


def test_console_help_for_command_uses_cli_summary():
    help_text = HermesConsoleEngine().help_text("skills list")

    assert help_text == "skills list\nList installed skills"


def test_console_registry_covers_non_admin_cli_surface():
    registered = set(HermesConsoleEngine().commands)

    missing = EXPECTED_CONSOLE_COMMANDS - registered

    assert missing == set()


# Regression: argparse's --help/--version action calls parser.exit() ->
# sys.exit(). The dashboard web console runs each command in a worker thread
# whose SystemExit (a BaseException) sailed past the `except Exception` guard,
# escaped the asyncio Task, and tore down the whole uvicorn process. The console
# must instead return the help text at the prompt. See console_engine
# _ArgumentParser.exit() + HermesConsoleEngine.execute().
CONSOLE_HELP_FLAG_LINES = [
    ("version --help", False),
    ("version -h", False),
    ("skills list --help", False),
    ("mcp add --help", True),  # mutating -> reached only on the confirmed pass
]


@pytest.mark.parametrize("line, confirmed", CONSOLE_HELP_FLAG_LINES)
def test_console_help_flag_returns_help_without_exiting(
    line: str, confirmed: bool, capsys: pytest.CaptureFixture[str]
):
    engine = HermesConsoleEngine()

    result = engine.execute(line, confirmed=confirmed)

    # Reaching this line at all proves no SystemExit escaped execute().
    assert isinstance(result, ConsoleResult)
    assert result.status == "ok"
    assert "usage:" in result.output
    # The help text is surfaced in the result, not dumped to the process
    # stdout/stderr (which the web console never shows the user).
    captured = capsys.readouterr()
    assert "usage:" not in captured.out
    assert "usage:" not in captured.err


def test_console_cron_create_help_does_not_crash_process():
    # Exact repro from the bug report. `cron create` is mutating, so the crash
    # happened on the confirmation pass (confirmed=True), when the handler
    # actually parsed the args and argparse hit --help.
    engine = HermesConsoleEngine()

    pending = engine.execute("cron create --help")
    assert pending.status == "confirm_required"

    result = engine.execute("cron create --help", confirmed=True)

    assert result.status == "ok"
    assert "usage: hermes cron create" in result.output


def _selftest_exit_handler(_engine: HermesConsoleEngine, _args: list[str]) -> str:
    raise SystemExit(7)


def test_console_contains_handler_process_exit():
    # Defense-in-depth: a handler (or a parser we don't control) that calls
    # sys.exit() must not let SystemExit escape execute(); that BaseException
    # would slip past the dashboard worker's `except Exception` and crash the
    # event loop. execute() must always return a ConsoleResult.
    engine = HermesConsoleEngine()
    engine.register(
        ("selftest-exit",),
        "selftest-exit",
        "Raise SystemExit for the containment test.",
        _selftest_exit_handler,
    )

    result = engine.execute("selftest-exit")

    assert isinstance(result, ConsoleResult)
    assert result.status == "error"
    assert "status 7" in result.output


EXPECTED_HOSTED_CONSOLE_COMMANDS = {
    ("status",),
    ("doctor",),
    ("logs",),
    ("version",),
    ("prompt-size",),
    ("insights",),
    ("security", "audit"),
    ("portal", "info"),
    ("portal", "tools"),
    ("send",),
    ("config", "show"),
    ("config", "path"),
    ("config", "env-path"),
    ("config", "check"),
    ("config", "migrate"),
    ("config", "set"),
    ("sessions", "list"),
    ("sessions", "stats"),
    ("sessions", "export"),
    ("sessions", "rename"),
    ("sessions", "optimize"),
    ("sessions", "repair"),
    ("cron", "list"),
    ("cron", "status"),
    ("cron", "create"),
    ("cron", "edit"),
    ("cron", "pause"),
    ("cron", "resume"),
    ("cron", "run"),
    ("cron", "remove"),
    ("cron", "tick"),
    ("profile",),
    ("profile", "list"),
    ("profile", "show"),
    ("profile", "info"),
    ("tools", "list"),
    ("tools", "enable"),
    ("tools", "disable"),
    ("tools", "post-setup"),
    ("skills", "browse"),
    ("skills", "search"),
    ("skills", "inspect"),
    ("skills", "list"),
    ("skills", "check"),
    ("skills", "list-modified"),
    ("skills", "diff"),
    ("skills", "install"),
    ("skills", "update"),
    ("skills", "audit"),
    ("skills", "uninstall"),
    ("skills", "reset"),
    ("skills", "opt-in"),
    ("skills", "opt-out"),
    ("skills", "repair-official"),
    ("skills", "snapshot", "export"),
    ("skills", "tap", "list"),
    ("mcp", "list"),
    ("mcp", "catalog"),
    ("mcp", "test"),
    ("mcp", "add"),
    ("mcp", "remove"),
    ("mcp", "install"),
    ("mcp", "login"),
    ("mcp", "reauth"),
    ("mcp", "configure"),
    ("mcp", "picker"),
    ("memory", "status"),
    ("auth", "list"),
    ("auth", "status"),
    ("auth", "reset"),
    ("auth", "spotify", "status"),
    ("pairing", "list"),
    ("pairing", "approve"),
    ("pairing", "revoke"),
    ("pairing", "clear-pending"),
    ("webhook", "list"),
    ("webhook", "subscribe"),
    ("webhook", "remove"),
    ("webhook", "test"),
}


def test_hosted_console_registry_exposes_only_hosted_safe_surface():
    engine = HermesConsoleEngine(context="hosted")
    hosted = {
        path for path, command in engine.commands.items() if "hosted" in command.contexts
    }

    assert hosted == EXPECTED_HOSTED_CONSOLE_COMMANDS


@pytest.mark.parametrize(
    "line",
    [
        "portal login",
        "auth add nous --type oauth",
        "auth logout nous",
        "profile create tester",
        "profile use default",
        "plugins list",
        "plugins install owner/repo",
        "kanban list",
        "hooks list",
        "checkpoints clear",
        "curator pause",
        "pets install cat",
        "backup --quick",
        "import /tmp/hermes-console-test.zip",
        "mcp serve",
        "model",
        "setup",
        "dashboard",
        "gateway restart",
        "update",
        "uninstall",
    ],
)
def test_hosted_console_rejects_local_only_or_dangerous_commands(line):
    result = HermesConsoleEngine(context="hosted").execute(line)

    assert result.status == "error"
    assert result.output


@pytest.mark.parametrize(
    "line",
    [
        "mcp add demo --url https://example.com/sse",
        "mcp install n8n",
        "mcp configure github",
        "mcp picker",
        "config set display.interface cli",
        "cron create 'every 1h' 'say hello'",
    ],
)
def test_hosted_console_allows_guarded_useful_commands_before_confirmation(line):
    result = HermesConsoleEngine(context="hosted").execute(line)

    assert result.status == "confirm_required"


@pytest.mark.parametrize(
    "line",
    [
        "mcp add local --command npx --args foo",
        "mcp add local --preset unsafe",
        "mcp add local --url file:///tmp/server",
        "config set model.provider openrouter",
        "config set portal.url https://evil.example",
        "cron create 'every 1h' 'say hello' --script scripts/ping.py",
        "cron create 'every 1h' 'say hello' --no-agent",
        "cron edit abc123 --workdir /tmp/project",
    ],
)
def test_hosted_console_blocks_known_footgun_arguments_before_confirmation(line):
    result = HermesConsoleEngine(context="hosted").execute(line)

    assert result.status == "error"
    assert result.output


@pytest.mark.parametrize(
    "line",
    [
        "sessions delete abc123",
        "sessions prune --older-than 1",
        "chat",
        "--cli",
        "--tui",
        "oneshot hello",
        "model",
        "setup",
        "postinstall",
        "fallback add",
        "moa configure",
        "claw migrate",
        "gateway restart",
        "gateway start",
        "gateway stop",
        "dashboard",
        "serve",
        "proxy start",
        "mcp serve",
        "skills config",
        "skills publish ./skill",
        "completion bash",
        "acp",
        "update",
        "uninstall",
        "gui",
        "desktop",
        "login",
        "logout",
        "--tui",
        "logs | cat",
        "config show > out.txt",
    ],
)
def test_console_rejects_destructive_and_shell_like_commands(line):
    result = HermesConsoleEngine().execute(line)

    assert result.status == "error"
    assert result.output


@pytest.mark.parametrize("line", MUTATING_CONFIRMATION_SMOKE_COMMANDS)
def test_mutating_console_commands_require_confirmation(line):
    result = HermesConsoleEngine().execute(line)

    assert result.status == "confirm_required"
    assert result.confirmation_message


def test_help_lists_supported_commands_and_not_full_cli():
    result = HermesConsoleEngine().execute("help")

    assert result.status == "ok"
    assert "sessions list" in result.output
    assert "config set" in result.output
    assert "dashboard" not in result.output
    assert "gateway restart" not in result.output


def test_config_set_requires_confirmation_then_writes(_isolate_hermes_home):
    engine = HermesConsoleEngine()

    # Use a schema-known key path. Since #34067, `config set` refuses unknown
    # top-level keys, so this flow test must target a valid path (telegram is a
    # PlatformConfig-shaped dict that accepts arbitrary child keys).
    pending = engine.execute("config set telegram.test true")
    assert pending.status == "confirm_required"

    from hermes_cli.config import read_raw_config

    assert read_raw_config() == {}

    result = engine.execute("config set telegram.test true", confirmed=True)

    assert result.status == "ok"
    assert "telegram.test" in result.output
    assert read_raw_config()["telegram"]["test"] is True


def test_sessions_list_and_stats_use_isolated_session_store(_isolate_hermes_home):
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        db.create_session("chat-session", source="cli", model="test/model")
        db.create_session("tool-session", source="tool", model="test/model")
    finally:
        db.close()

    engine = HermesConsoleEngine()
    listed = engine.execute("sessions list --limit 10")
    stats = engine.execute("sessions stats")

    assert listed.status == "ok"
    assert "chat-session" in listed.output
    assert "tool-session" not in listed.output
    assert "Total sessions: 2" in stats.output
    assert "Listable sessions: 1" in stats.output


@pytest.fixture()
def _tmp_cron_store(tmp_path, monkeypatch):
    """Redirect cron.jobs' module-pinned storage to a per-test tmp dir.

    ``cron.jobs.CRON_DIR``/``JOBS_FILE``/``OUTPUT_DIR`` are constants resolved
    from ``get_hermes_home()`` at *import* time — which, during collection,
    is the real ``~/.hermes`` (the hermetic ``HERMES_HOME`` env override lands
    after import). Without this, a console cron test creating a named job
    writes it to the live cron store: it pollutes the user's real jobs.json
    and, under xdist, goes ambiguous once a sibling reuses the same name.
    Mirrors ``tests/cron/test_jobs.py::tmp_cron_dir``.
    """
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


def test_cron_pause_resume_and_run_require_confirmation(
    _isolate_hermes_home, _tmp_cron_store, monkeypatch: pytest.MonkeyPatch
):
    from cron.jobs import create_job, get_job

    # Local `cron run` now executes immediately (CLI/LLM parity); stub the real
    # agent-run boundary so this confirmation-flow test doesn't depend on — or
    # attempt — an actual agent execution.
    def _fake_run_one_job(job):
        from cron.jobs import mark_job_run

        mark_job_run(job["id"], True)
        return True

    monkeypatch.setattr("cron.scheduler.run_one_job", _fake_run_one_job)

    job = create_job(prompt="say hello", schedule="every 1h", name="alpha")
    engine = HermesConsoleEngine()

    pending = engine.execute(f"cron pause {job['id']}")
    assert pending.status == "confirm_required"
    stored = get_job(job["id"])
    assert stored is not None
    assert stored["state"] == "scheduled"

    paused = engine.execute(f"cron pause {job['id']}", confirmed=True)
    assert paused.status == "ok"
    stored = get_job(job["id"])
    assert stored is not None
    assert stored["state"] == "paused"

    resumed = engine.execute("cron resume alpha", confirmed=True)
    assert resumed.status == "ok"
    stored = get_job(job["id"])
    assert stored is not None
    assert stored["state"] == "scheduled"

    triggered = engine.execute("cron run alpha", confirmed=True)
    assert triggered.status == "ok"
    assert "Ran job" in triggered.output


def test_cron_run_attributes_trigger_to_console(
    _isolate_hermes_home, _tmp_cron_store, monkeypatch: pytest.MonkeyPatch
):
    """`cron run` must emit CRON_TRIGGERED with an explicit console caller.

    The emit fires on both the local execute-now path and the hosted
    schedule-for-next-tick path; this exercises the default (local) engine.
    """
    from cron.jobs import create_job
    from events.bus import EventBus
    from events.schema import EventType

    # ``_tmp_cron_store`` isolates the module-pinned cron paths (returning the
    # tmp root); point the emit-side bus at a tmp DB in that same root.
    bus = EventBus(db_path=_tmp_cron_store / "events.db")
    monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

    # Local `cron run` executes immediately; stub the agent-run boundary so the
    # attribution assertions don't depend on a real agent execution.
    monkeypatch.setattr("cron.scheduler.run_one_job", lambda job: True)

    job = create_job(prompt="say hello", schedule="every 1h", name="alpha")
    engine = HermesConsoleEngine()

    triggered = engine.execute("cron run alpha", confirmed=True)
    assert triggered.status == "ok"

    events = bus.query(event_type=EventType.CRON_TRIGGERED)
    assert len(events) == 1
    assert events[0].payload["caller"] == "tui:console_engine"
    assert events[0].payload["job_id"] == job["id"]


def test_cron_run_executes_immediately_in_local_context(
    _isolate_hermes_home, _tmp_cron_store, monkeypatch: pytest.MonkeyPatch
):
    """Local REPL `cron run` executes the job NOW (CLI/LLM `run` parity), not
    just schedule-for-next-tick — and still emits CRON_TRIGGERED as the console.

    A manual `cron run` in the standalone REPL should actually fire, even when
    no gateway ticker is active (the #41037 case), mirroring `hermes cron run`
    and the cronjob(action='run') tool which both route through _execute_job_now.
    """
    from cron.jobs import create_job
    from events.bus import EventBus
    from events.schema import EventType

    # ``_tmp_cron_store`` isolates the module-pinned cron paths (returning the
    # tmp root); point the emit-side bus at a tmp DB in that same root.
    bus = EventBus(db_path=_tmp_cron_store / "events.db")
    monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

    # Stub the real agent-execution boundary so no actual run happens: record
    # that it fired and mark the job ok so the console reports success.
    ran: dict = {}

    def _fake_run_one_job(job):
        from cron.jobs import mark_job_run

        ran["job_id"] = job["id"]
        mark_job_run(job["id"], True)
        return True

    monkeypatch.setattr("cron.scheduler.run_one_job", _fake_run_one_job)

    job = create_job(prompt="say hello", schedule="every 1h", name="alpha")
    engine = HermesConsoleEngine()  # default context == "local"
    assert engine.context == "local"

    result = engine.execute("cron run alpha", confirmed=True)
    assert result.status == "ok"
    assert "Ran job" in result.output
    assert "succeeded" in result.output

    # It actually executed now, rather than deferring to a scheduler tick.
    assert ran.get("job_id") == job["id"]

    # Attribution is preserved on the execute-now path.
    events = bus.query(event_type=EventType.CRON_TRIGGERED)
    assert len(events) == 1
    assert events[0].payload["caller"] == "tui:console_engine"
    assert events[0].payload["job_id"] == job["id"]


def test_cron_run_reports_skip_when_claim_lost_in_local_context(
    _isolate_hermes_home, _tmp_cron_store, monkeypatch: pytest.MonkeyPatch
):
    """If the scheduler already holds the fire claim, local `cron run` reports a
    skip and never double-runs the job (at-most-once safety)."""
    from cron.jobs import create_job

    # Force the at-most-once claim to be lost (another fire owns this job).
    monkeypatch.setattr(
        "tools.cronjob_tools.claim_job_for_fire", lambda job_id: False
    )

    def _must_not_run(job):
        raise AssertionError("run_one_job must not fire when the claim is lost")

    monkeypatch.setattr("cron.scheduler.run_one_job", _must_not_run)

    create_job(prompt="say hello", schedule="every 1h", name="alpha")
    engine = HermesConsoleEngine()  # local

    result = engine.execute("cron run alpha", confirmed=True)
    assert result.status == "ok"
    assert "Run skipped" in result.output
    assert "already being fired" in result.output


def test_cron_run_executes_in_background_in_hosted_context(
    _isolate_hermes_home, _tmp_cron_store, monkeypatch: pytest.MonkeyPatch
):
    """Hosted (dashboard web-console) `cron run` fires the job NOW, but off the
    console's bounded 4-worker/60s pool: it dispatches `_execute_job_now` on a
    dedicated background executor and returns a "started" ack immediately.

    The run still emits exactly one CRON_TRIGGERED (console caller) and — via
    `run_one_job` -> `on_job_completed` — the completion event the dashboard
    activity feed already consumes; that is how the result is surfaced without
    blocking the console thread or tripping the 60s timeout.
    """
    import concurrent.futures

    from cron.jobs import create_job, get_job
    from events.bus import EventBus
    from events.schema import EventType
    from hermes_cli import console_engine

    bus = EventBus(db_path=_tmp_cron_store / "events.db")
    monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

    # Stub the real agent-execution boundary: record that it fired and mark the
    # job ok. Running on the background thread, it reaches the same store/bus
    # (HERMES_HOME env + monkeypatched globals are process-global).
    ran: dict = {}

    def _fake_run_one_job(job):
        from cron.jobs import mark_job_run

        ran["job_id"] = job["id"]
        mark_job_run(job["id"], True)
        return True

    monkeypatch.setattr("cron.scheduler.run_one_job", _fake_run_one_job)

    # The test owns the background executor so it can join deterministically
    # (no sleeps): shutdown(wait=True) blocks until the fire completes.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(
        console_engine, "_get_console_run_executor", lambda: executor
    )

    job = create_job(prompt="say hello", schedule="every 1h", name="alpha")
    engine = HermesConsoleEngine(context="hosted")

    result = engine.execute("cron run alpha", confirmed=True)
    assert result.status == "ok"
    assert "Started" in result.output
    assert "background" in result.output.lower()
    assert job["id"] in result.output

    executor.shutdown(wait=True)  # join the background fire

    # It actually executed NOW in the background (not defer-to-tick).
    assert ran.get("job_id") == job["id"]
    stored = get_job(job["id"])
    assert stored is not None
    assert stored.get("last_status") == "ok"

    # Exactly one CRON_TRIGGERED, attributed to the console caller.
    events = bus.query(event_type=EventType.CRON_TRIGGERED)
    assert len(events) == 1
    assert events[0].payload["caller"] == "tui:console_engine"
    assert events[0].payload["job_id"] == job["id"]


def test_cron_run_hosted_dispatch_is_non_blocking(
    _isolate_hermes_home, _tmp_cron_store, monkeypatch: pytest.MonkeyPatch
):
    """The hosted `cron run` ack returns while the agent run is still in flight.

    A synchronous run would block the console worker (and trip the 60s timeout).
    Here the stubbed run parks on a gate: if `execute()` returned, the console
    thread was NOT blocked on the run. If it *were* blocked, the test would
    deadlock (the gate is released only after `execute()` returns) — a hang is
    the failure signal.
    """
    import concurrent.futures
    import threading

    from cron.jobs import create_job
    from events.bus import EventBus
    from hermes_cli import console_engine

    bus = EventBus(db_path=_tmp_cron_store / "events.db")
    monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

    started = threading.Event()
    gate = threading.Event()

    def _blocking_run_one_job(job):
        from cron.jobs import mark_job_run

        started.set()
        if not gate.wait(timeout=5):
            raise AssertionError("gate never released")
        mark_job_run(job["id"], True)
        return True

    monkeypatch.setattr("cron.scheduler.run_one_job", _blocking_run_one_job)

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(
        console_engine, "_get_console_run_executor", lambda: executor
    )

    create_job(prompt="say hello", schedule="every 1h", name="alpha")
    engine = HermesConsoleEngine(context="hosted")

    # Returns immediately with the ack even though the run is parked on `gate`.
    result = engine.execute("cron run alpha", confirmed=True)
    assert result.status == "ok"
    assert "background" in result.output.lower()

    # The background fire genuinely started, and it is still blocked — proving
    # `execute()` did not wait for it.
    assert started.wait(timeout=5)
    assert not gate.is_set()

    gate.set()
    executor.shutdown(wait=True)


def test_cron_run_hosted_missing_job_fast_fails_without_dispatch(
    _isolate_hermes_home, _tmp_cron_store, monkeypatch: pytest.MonkeyPatch
):
    """Hosted `cron run <missing>` resolves on the console thread and fast-fails
    with `Job not found` — it must NOT dispatch a background run for a job that
    does not exist."""
    from hermes_cli import console_engine

    class _NoDispatchExecutor:
        def submit(self, *args, **kwargs):
            raise AssertionError(
                "must not dispatch a background run for a missing job"
            )

    monkeypatch.setattr(
        console_engine, "_get_console_run_executor", lambda: _NoDispatchExecutor()
    )

    engine = HermesConsoleEngine(context="hosted")
    result = engine.execute("cron run does-not-exist", confirmed=True)
    assert result.status == "error"
    assert "not found" in result.output.lower()


def test_cron_run_hosted_returns_started_ack_even_when_claim_lost(
    _isolate_hermes_home, _tmp_cron_store, monkeypatch: pytest.MonkeyPatch
):
    """The hosted background fire is optimistic (fire-and-forget): if the
    scheduler already holds the fire claim, `_execute_job_now` no-ops on the
    background thread, but the console has already returned the "started" ack.
    At-most-once safety still holds — `run_one_job` is never called — and the
    activity feed shows the scheduler's own trigger/completion. This pins that
    deliberate optimistic-ack decision.
    """
    import concurrent.futures

    from cron.jobs import create_job
    from hermes_cli import console_engine

    # Force the at-most-once claim to be lost (another fire owns this job).
    monkeypatch.setattr(
        "tools.cronjob_tools.claim_job_for_fire", lambda job_id: False
    )

    def _must_not_run(job):
        raise AssertionError("run_one_job must not fire when the claim is lost")

    monkeypatch.setattr("cron.scheduler.run_one_job", _must_not_run)

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(
        console_engine, "_get_console_run_executor", lambda: executor
    )

    create_job(prompt="say hello", schedule="every 1h", name="alpha")
    engine = HermesConsoleEngine(context="hosted")

    result = engine.execute("cron run alpha", confirmed=True)
    assert result.status == "ok"
    assert "Started" in result.output
    assert "background" in result.output.lower()

    executor.shutdown(wait=True)  # join; the fire no-ops on a lost claim


def test_cron_run_hosted_background_fire_inherits_profile_home(
    _isolate_hermes_home,
    _tmp_cron_store,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    """The background fire must resolve the SAME profile store as the console
    thread that dispatched it.

    Hosted `cron run` runs inside `_profile_scope`, which sets a *context-local*
    (ContextVar) HERMES_HOME override for a non-default profile. A
    ThreadPoolExecutor worker does NOT inherit that ContextVar, so the dispatch
    must carry the caller's context (contextvars.copy_context) — otherwise the
    fire would claim/run/record against the WRONG profile's store, regressing
    the per-profile fire correctness cron.jobs' dynamic resolution preserves.
    """
    import concurrent.futures

    from cron.jobs import create_job
    from events.bus import EventBus
    from hermes_cli import console_engine
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    bus = EventBus(db_path=_tmp_cron_store / "events.db")
    monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

    # A distinct profile home, different from the hermetic default HERMES_HOME.
    profile_home = tmp_path / "profileX"
    (profile_home / "cron").mkdir(parents=True)

    seen: dict = {}

    def _fake_run_one_job(job):
        from cron.jobs import _get_hermes_home, mark_job_run

        seen["home"] = str(_get_hermes_home().resolve())
        mark_job_run(job["id"], True)
        return True

    monkeypatch.setattr("cron.scheduler.run_one_job", _fake_run_one_job)

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(
        console_engine, "_get_console_run_executor", lambda: executor
    )

    # Enter the profile override on THIS (console) thread, create the job in the
    # profile store, and dispatch — mirroring `_profile_scope` wrapping a hosted
    # console command.
    token = set_hermes_home_override(str(profile_home))
    try:
        create_job(prompt="say hello", schedule="every 1h", name="alpha")
        engine = HermesConsoleEngine(context="hosted")
        result = engine.execute("cron run alpha", confirmed=True)
        assert result.status == "ok"
    finally:
        reset_hermes_home_override(token)

    executor.shutdown(wait=True)  # join the background fire

    # The background thread resolved the PROFILE store, not the process default.
    assert seen.get("home") == str(profile_home.resolve())


def test_repl_runs_non_interactive_lines_without_prompts(_isolate_hermes_home):
    stdin = io.StringIO("help\nexit\n")
    stdout = io.StringIO()
    stderr = io.StringIO()

    code = run_console_repl(
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        interactive=False,
    )

    assert code == 0
    assert "Hermes Console" in stdout.getvalue()
    assert "hermes>" not in stdout.getvalue()
    assert stderr.getvalue() == ""


def test_repl_refuses_non_interactive_confirmation(_isolate_hermes_home):
    stdin = io.StringIO("config set console.test true\n")
    stdout = io.StringIO()
    stderr = io.StringIO()

    code = run_console_repl(
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        interactive=False,
    )

    assert code == 1
    assert "Confirmation required" in stderr.getvalue()


def test_main_console_subcommand_smoke(_isolate_hermes_home):
    import subprocess

    result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "console"],
        cwd=Path(__file__).resolve().parents[2],
        input="help\nexit\n",
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0
    assert "Hermes Console" in result.stdout
