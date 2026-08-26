"""Tests for hermes_cli.cron command handling."""

from argparse import Namespace
from types import SimpleNamespace

import pytest

from cron.jobs import create_job, get_job, list_jobs
from hermes_cli import cron as cron_cli
from hermes_cli.cron import cron_command


def _parse_cron_args(argv):
    """Parse ``argv`` through the REAL ``hermes cron`` parser.

    A hand-built ``Namespace`` sets the attribute the handler reads, so it
    passes whether or not the flag exists on the parser — which is exactly how
    a missing ``--reason`` survived until d786a92f78. Anything asserting that a
    FLAG works must come through here.
    """
    import argparse

    from hermes_cli.subcommands.cron import build_cron_parser

    parser = argparse.ArgumentParser(prog="hermes")
    build_cron_parser(parser.add_subparsers(dest="command"), cmd_cron=lambda a: 0)
    return parser.parse_args(argv)


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


class TestCronCommandLifecycle:
    def test_pause_resume_run(self, tmp_cron_dir, capsys, monkeypatch):
        from events.bus import EventBus
        from events.schema import EventType

        bus = EventBus(db_path=tmp_cron_dir / "events.db")
        monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

        job = create_job(prompt="Check server status", schedule="every 1h")

        cron_command(Namespace(cron_command="pause", job_id=job["id"]))
        paused = get_job(job["id"])
        assert paused["state"] == "paused"

        cron_command(Namespace(cron_command="resume", job_id=job["id"]))
        resumed = get_job(job["id"])
        assert resumed["state"] == "scheduled"

        cron_command(
            Namespace(
                cron_command="run",
                job_id=job["id"],
                reason="investigation 2026-04-30",
            )
        )
        triggered = get_job(job["id"])
        assert triggered["state"] == "scheduled"

        events = bus.query(event_type=EventType.CRON_TRIGGERED)
        assert len(events) == 1
        assert events[0].payload["caller"] == "hermes_cli:cron_run"
        assert events[0].payload["reason"] == "investigation 2026-04-30"

        out = capsys.readouterr().out
        assert "Paused job" in out
        assert "Resumed job" in out
        assert "Triggered job" in out

    def test_pause_reason_is_persisted_and_echoed(self, tmp_cron_dir, capsys):
        """`hermes cron pause <id> --reason ...` records the WHY.

        Without it a paused job is indistinguishable from one paused because it
        is broken, and the next session has to guess whether resuming is safe.
        """
        job = create_job(prompt="Check server status", schedule="every 1h")

        rc = cron_command(
            Namespace(
                cron_command="pause",
                job_id=job["id"],
                reason="host CPU-saturated 2026-08-23",
            )
        )

        assert rc == 0
        paused = get_job(job["id"])
        assert paused["state"] == "paused"
        assert paused["paused_reason"] == "host CPU-saturated 2026-08-23"
        assert "Reason: host CPU-saturated 2026-08-23" in capsys.readouterr().out

    def test_pause_blank_reason_is_stored_as_none(self, tmp_cron_dir, capsys):
        job = create_job(prompt="Check server status", schedule="every 1h")

        cron_command(
            Namespace(cron_command="pause", job_id=job["id"], reason="   ")
        )

        assert get_job(job["id"])["paused_reason"] is None
        assert "(none recorded" in capsys.readouterr().out

    def test_pause_without_a_reason_attribute_still_works(self, tmp_cron_dir, capsys):
        """Back-compat: callers that build the Namespace by hand omit `reason`."""
        job = create_job(prompt="Check server status", schedule="every 1h")

        rc = cron_command(Namespace(cron_command="pause", job_id=job["id"]))

        assert rc == 0
        assert get_job(job["id"])["paused_reason"] is None
        assert "(none recorded" in capsys.readouterr().out

    def test_list_surfaces_the_pause_reason(self, tmp_cron_dir, capsys):
        """Storing the reason is only useful if an audit can see it."""
        job = create_job(prompt="Check server status", schedule="every 1h")
        cron_command(
            Namespace(
                cron_command="pause",
                job_id=job["id"],
                reason="host CPU-saturated 2026-08-23",
            )
        )
        capsys.readouterr()

        cron_command(Namespace(cron_command="list", all=True))

        out = capsys.readouterr().out
        assert "[paused]" in out
        assert "Paused:    host CPU-saturated 2026-08-23 (since " in out

    def test_list_flags_a_pause_with_no_reason(self, tmp_cron_dir, capsys):
        job = create_job(prompt="Check server status", schedule="every 1h")
        cron_command(Namespace(cron_command="pause", job_id=job["id"]))
        capsys.readouterr()

        cron_command(Namespace(cron_command="list", all=True))

        assert "Paused:    (no reason recorded)" in capsys.readouterr().out

    def test_run_reason_from_the_real_parser_reaches_the_audit_event(
        self, tmp_cron_dir, monkeypatch
    ):
        """`hermes cron run <id> --reason ...` attributes the off-schedule fire.

        The dispatch always forwarded ``args.reason`` into CRON_TRIGGERED, but
        the run subparser had no ``--reason``, so from a terminal the field was
        unreachable and every hand-triggered run wrote an unattributed event.
        Driven through the REAL parser on purpose — a hand-built Namespace
        cannot catch a missing flag.
        """
        import argparse

        from events.bus import EventBus
        from events.schema import EventType
        from hermes_cli.subcommands.cron import build_cron_parser

        bus = EventBus(db_path=tmp_cron_dir / "events.db")
        monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

        job = create_job(prompt="Check server status", schedule="every 1h")

        parser = argparse.ArgumentParser(prog="hermes")
        build_cron_parser(parser.add_subparsers(dest="command"), cmd_cron=lambda a: 0)
        args = parser.parse_args(
            ["cron", "run", job["id"], "--reason", "manual retry after CPU spike"]
        )
        assert args.reason == "manual retry after CPU spike"

        assert cron_command(args) == 0

        events = bus.query(event_type=EventType.CRON_TRIGGERED)
        assert len(events) == 1
        assert events[0].payload["caller"] == "hermes_cli:cron_run"
        assert events[0].payload["reason"] == "manual retry after CPU spike"

    def test_run_blank_reason_is_recorded_as_none(self, tmp_cron_dir, monkeypatch):
        """Whitespace is not attribution — same normalization as pause."""
        from events.bus import EventBus
        from events.schema import EventType

        bus = EventBus(db_path=tmp_cron_dir / "events.db")
        monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

        job = create_job(prompt="Check server status", schedule="every 1h")
        cron_command(Namespace(cron_command="run", job_id=job["id"], reason="   "))

        events = bus.query(event_type=EventType.CRON_TRIGGERED)
        assert len(events) == 1
        assert events[0].payload["reason"] is None

    def test_edit_model_and_provider_from_the_real_parser_reach_the_store(
        self, tmp_cron_dir, capsys
    ):
        """`hermes cron edit <id> --model ... --provider ...` pins the job.

        The per-job model/provider pair is the highest-precedence and most
        durable inference override on the box, but `cron edit` defined no flags
        for it, so an operator could not set it from a terminal at all. Driven
        through the REAL parser on purpose — a hand-built Namespace supplies
        `args.model` itself and so cannot catch a missing flag.
        """
        job = create_job(prompt="Check server status", schedule="every 1h")
        assert get_job(job["id"]).get("model") is None

        args = _parse_cron_args(
            ["cron", "edit", job["id"], "--model", "claude-opus-5",
             "--provider", "anthropic"]
        )
        assert args.model == "claude-opus-5"
        assert args.provider == "anthropic"

        assert cron_command(args) == 0

        pinned = get_job(job["id"])
        assert pinned["model"] == "claude-opus-5"
        assert pinned["provider"] == "anthropic"

        out = capsys.readouterr().out
        assert "Model: claude-opus-5" in out
        assert "Provider: anthropic" in out

    def test_edit_leaves_an_existing_pin_alone_when_the_flags_are_omitted(
        self, tmp_cron_dir
    ):
        """A bare edit must not silently unpin the job."""
        job = create_job(
            prompt="Check server status",
            schedule="every 1h",
            model="claude-opus-5",
            provider="anthropic",
        )

        assert cron_command(
            _parse_cron_args(["cron", "edit", job["id"], "--name", "Renamed"])
        ) == 0

        still = get_job(job["id"])
        assert still["name"] == "Renamed"
        assert still["model"] == "claude-opus-5"
        assert still["provider"] == "anthropic"

    def test_edit_clears_the_pin_with_an_empty_value_and_says_so(
        self, tmp_cron_dir, capsys
    ):
        """`--model ""` unpins, and the clear is echoed.

        Same empty-string-clears convention as --script/--workdir. The echo
        matters: a clear that printed nothing would be indistinguishable from
        a no-op on the one field a git checkout cannot restore.
        """
        job = create_job(
            prompt="Check server status",
            schedule="every 1h",
            model="claude-opus-5",
            provider="anthropic",
        )

        assert cron_command(
            _parse_cron_args(
                ["cron", "edit", job["id"], "--model", "", "--provider", ""]
            )
        ) == 0

        cleared = get_job(job["id"])
        assert cleared["model"] is None
        assert cleared["provider"] is None

        out = capsys.readouterr().out
        assert "Model: (inherited from env/config)" in out
        assert "Provider: (inherited from env/config)" in out

    def test_edit_pin_with_no_base_url_passes_the_exfil_guard(self, tmp_cron_dir):
        """Setting model/provider alone (base_url None) must not trip F8.

        `_validate_cron_base_url` re-runs on every update; with no base_url on
        either side of the merge there is nothing to refuse, and a false
        refusal here would make the new flags unusable for their main case.
        """
        job = create_job(prompt="Check server status", schedule="every 1h")
        assert get_job(job["id"]).get("base_url") is None

        assert cron_command(
            _parse_cron_args(["cron", "edit", job["id"], "--provider", "anthropic"])
        ) == 0
        assert get_job(job["id"])["provider"] == "anthropic"

    def test_edit_provider_is_refused_when_the_stored_base_url_would_leak(
        self, tmp_cron_dir, capsys
    ):
        """The new flags do not open a hole around the F8 credential guard.

        A job persisted with an off-host base_url plus a named provider would,
        on fire, send that provider's stored API key to the attacker endpoint
        (CWE-200). `cron edit` routes through `cronjob(action="update")`, which
        validates the EFFECTIVE pair — so naming a real provider from the CLI
        is refused and the stored record is left untouched.
        """
        job = create_job(
            prompt="Check server status",
            schedule="every 1h",
            base_url="https://evil.example",
        )

        assert cron_command(
            _parse_cron_args(["cron", "edit", job["id"], "--provider", "anthropic"])
        ) == 1

        unchanged = get_job(job["id"])
        assert unchanged.get("provider") is None
        assert "not allowed for provider" in capsys.readouterr().out

    def test_list_renders_the_model_and_provider_pin(self, tmp_cron_dir, capsys):
        """An operator must be able to SEE which jobs carry a pin."""
        create_job(
            prompt="Pinned job",
            schedule="every 1h",
            model="claude-opus-5",
            provider="anthropic",
            base_url="https://api.anthropic.com",
        )

        cron_command(Namespace(cron_command="list"))

        out = capsys.readouterr().out
        assert "Model:     claude-opus-5" in out
        assert "Provider:  anthropic" in out
        assert "Base URL:  https://api.anthropic.com" in out

    def test_list_omits_the_pin_lines_for_an_unpinned_job(self, tmp_cron_dir, capsys):
        """No pin, no line — an empty label would read as a pin to nothing."""
        create_job(prompt="Unpinned job", schedule="every 1h")

        cron_command(Namespace(cron_command="list"))

        out = capsys.readouterr().out
        assert "Model:" not in out
        assert "Provider:" not in out
        assert "Base URL:" not in out

    def test_create_model_and_provider_from_the_real_parser_pin_the_new_job(
        self, tmp_cron_dir, capsys
    ):
        """`hermes cron create ... --model X --provider Y` pins at creation.

        Same missing-argparse-surface gap as `cron edit`: `cronjob(action=
        "create")` already accepted model/provider, but the create subparser
        defined neither, so a job could only be pinned by creating it unpinned
        and editing it afterwards.
        """
        args = _parse_cron_args(
            ["cron", "create", "every 1h", "Pinned at birth",
             "--name", "born-pinned",
             "--model", "claude-opus-5", "--provider", "anthropic"]
        )
        assert args.model == "claude-opus-5"
        assert args.provider == "anthropic"

        assert cron_command(args) == 0

        job = next(j for j in list_jobs() if j["name"] == "born-pinned")
        assert job["model"] == "claude-opus-5"
        assert job["provider"] == "anthropic"

        out = capsys.readouterr().out
        assert "Model: claude-opus-5" in out
        assert "Provider: anthropic" in out

    def test_create_pinned_axes_carry_no_drift_snapshot(
        self, tmp_cron_dir, monkeypatch
    ):
        """A pinned axis must not also record a snapshot.

        `_compute_provider_model_snapshots` snapshots only UNPINNED axes; the
        drift guard then skips an unpinned job whose global config moved. So
        pinning at creation is not merely cosmetic — it is what exempts the job
        from that skip. Asserting the snapshot is absent pins that contract.

        The default-model resolver is stubbed rather than left to the host:
        under pytest it resolves to None anyway, which would make the control
        below vacuously true and the whole test unable to fail.
        """
        monkeypatch.setattr(
            "cron.jobs._resolve_default_model_snapshot", lambda: "sentinel-model"
        )

        assert cron_command(
            _parse_cron_args(
                ["cron", "create", "every 1h", "Pinned", "--name", "pinned",
                 "--model", "claude-opus-5", "--provider", "anthropic"]
            )
        ) == 0
        pinned = next(j for j in list_jobs() if j["name"] == "pinned")
        assert pinned.get("provider_snapshot") is None
        assert pinned.get("model_snapshot") is None

        # Control: an unpinned job on the same store DOES get snapshotted, so
        # the assertion above cannot pass merely because snapshotting is off.
        assert cron_command(
            _parse_cron_args(
                ["cron", "create", "every 1h", "Unpinned", "--name", "unpinned"]
            )
        ) == 0
        unpinned = next(j for j in list_jobs() if j["name"] == "unpinned")
        assert unpinned.get("model_snapshot") == "sentinel-model"

    def test_create_without_the_flags_leaves_the_job_unpinned(self, tmp_cron_dir):
        """The new flags must not invent a pin on a plain create."""
        assert cron_command(
            _parse_cron_args(["cron", "create", "every 1h", "Plain", "--name", "plain"])
        ) == 0
        job = next(j for j in list_jobs() if j["name"] == "plain")
        assert job.get("model") is None
        assert job.get("provider") is None

    def test_edit_can_replace_and_clear_skills(self, tmp_cron_dir, capsys):
        job = create_job(
            prompt="Combine skill outputs",
            schedule="every 1h",
            skill="blogwatcher",
        )

        cron_command(
            Namespace(
                cron_command="edit",
                job_id=job["id"],
                schedule="every 2h",
                prompt="Revised prompt",
                name="Edited Job",
                deliver=None,
                repeat=None,
                skill=None,
                skills=["maps", "blogwatcher"],
                clear_skills=False,
                add_skills=None,
                remove_skills=None,
                script=None,
                workdir=None,
                no_agent=None,
            )
        )
        updated = get_job(job["id"])
        assert updated["skills"] == ["maps", "blogwatcher"]
        assert updated["name"] == "Edited Job"
        assert updated["prompt"] == "Revised prompt"
        assert updated["schedule_display"] == "every 120m"

        cron_command(
            Namespace(
                cron_command="edit",
                job_id=job["id"],
                schedule=None,
                prompt=None,
                name=None,
                deliver=None,
                repeat=None,
                skill=None,
                skills=None,
                clear_skills=True,
                add_skills=None,
                remove_skills=None,
                script=None,
                workdir=None,
                no_agent=None,
            )
        )
        cleared = get_job(job["id"])
        assert cleared["skills"] == []
        assert cleared["skill"] is None

        out = capsys.readouterr().out
        assert "Updated job" in out

    def test_create_with_multiple_skills(self, tmp_cron_dir, capsys):
        cron_command(
            Namespace(
                cron_command="create",
                schedule="every 1h",
                prompt="Use both skills",
                name="Skill combo",
                deliver=None,
                repeat=None,
                skill=None,
                skills=["blogwatcher", "maps"],
                script=None,
                workdir=None,
                no_agent=False,
            )
        )
        out = capsys.readouterr().out
        assert "Created job" in out

        jobs = list_jobs()
        assert len(jobs) == 1
        assert jobs[0]["skills"] == ["blogwatcher", "maps"]
        assert jobs[0]["name"] == "Skill combo"

    def test_list_does_not_crash_when_repeat_is_null(self, tmp_cron_dir, capsys):
        """A one-shot job can be persisted with ``"repeat": null``. `cron
        list` must render it as ∞ rather than crashing on .get(...)\\.get."""
        from cron.jobs import load_jobs, save_jobs

        create_job(prompt="One shot", schedule="every 1h")
        # Force the present-but-null shape that .get("repeat", {}) mishandles.
        jobs = load_jobs()
        jobs[0]["repeat"] = None
        save_jobs(jobs)

        cron_command(Namespace(cron_command="list", all=True))

        out = capsys.readouterr().out
        assert "Repeat:    ∞" in out

    def test_list_does_not_crash_when_deliver_is_null(self, tmp_cron_dir, capsys):
        """A job can be persisted with ``"deliver": null`` (present-but-null).
        `cron list` must fall back to the default channel rather than crashing
        on ``", ".join(None)`` — same dict-default pitfall as ``repeat`` (#32896).
        """
        from cron.jobs import load_jobs, save_jobs

        create_job(prompt="No deliver", schedule="every 1h")
        jobs = load_jobs()
        jobs[0]["deliver"] = None
        save_jobs(jobs)

        cron_command(Namespace(cron_command="list", all=True))

        out = capsys.readouterr().out
        assert "Deliver:   local" in out


class TestGatewayNotRunningWarning:
    """`cron create` / `cron list` must warn when the gateway (and thus the
    cron ticker) isn't running, since jobs only fire inside the gateway.
    Regression guard for #51038 — the most common cron 'jobs never fired'
    report was simply a gateway that was never started.
    """

    def test_create_warns_when_gateway_absent(self, tmp_cron_dir, capsys, monkeypatch):
        monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda: [])
        cron_command(
            Namespace(
                cron_command="create",
                schedule="0 11 * * *",
                prompt="Daily report",
                name="Daily 1130",
                deliver=None,
                repeat=None,
                skill=None,
                skills=None,
                script=None,
                workdir=None,
                no_agent=False,
            )
        )
        out = capsys.readouterr().out
        assert "Created job" in out
        assert "Gateway is not running" in out

    def test_create_silent_when_gateway_running(self, tmp_cron_dir, capsys, monkeypatch):
        monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda: [4242])
        cron_command(
            Namespace(
                cron_command="create",
                schedule="0 11 * * *",
                prompt="Daily report",
                name="Daily 1130",
                deliver=None,
                repeat=None,
                skill=None,
                skills=None,
                script=None,
                workdir=None,
                no_agent=False,
            )
        )
        out = capsys.readouterr().out
        assert "Created job" in out
        assert "Gateway is not running" not in out

    def test_list_warns_when_gateway_absent(self, tmp_cron_dir, capsys, monkeypatch):
        create_job(prompt="Daily report", schedule="0 11 * * *")
        monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda: [])
        cron_command(Namespace(cron_command="list", all=True))
        out = capsys.readouterr().out
        assert "Gateway is not running" in out


class TestExternalCronProviderStatus:
    """With an external cron provider (e.g. Chronos), jobs fire via a
    NAS-mediated webhook, NOT the in-process ticker. The ticker-heartbeat /
    gateway-process heuristics are meaningless there, so neither
    `cron status` nor the create/list warning must claim the gateway being
    absent means jobs won't fire — that was a false-negative on every healthy
    Chronos instance (the heartbeat is intentionally never written).
    """

    def test_status_reports_provider_not_ticker_for_chronos(
        self, tmp_cron_dir, capsys, monkeypatch
    ):
        create_job(prompt="Ping", schedule="every 2m")
        monkeypatch.setattr(
            "hermes_cli.cron._active_cron_provider_name", lambda: "chronos"
        )
        # Even with NO gateway process and NO ticker heartbeat, Chronos status
        # must NOT report a stall / "not firing".
        monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda: [])
        cron_command(Namespace(cron_command="status"))
        out = capsys.readouterr().out
        assert "chronos" in out
        assert "managed scheduler" in out
        assert "not firing" not in out.lower()
        assert "STALLED" not in out
        assert "Gateway is not running" not in out
        # Still surfaces the active-job summary.
        assert "active job(s)" in out

    def test_status_unchanged_for_builtin(self, tmp_cron_dir, capsys, monkeypatch):
        create_job(prompt="Ping", schedule="every 2m")
        monkeypatch.setattr(
            "hermes_cli.cron._active_cron_provider_name", lambda: "builtin"
        )
        monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda: [])
        cron_command(Namespace(cron_command="status"))
        out = capsys.readouterr().out
        # Built-in path is the historical ticker-based report.
        assert "Gateway is not running" in out
        assert "managed scheduler" not in out

    def test_create_silent_for_chronos_even_without_gateway(
        self, tmp_cron_dir, capsys, monkeypatch
    ):
        # The create-time "gateway not running" nag is a ticker-only concern;
        # an external provider doesn't depend on a live in-process ticker.
        monkeypatch.setattr(
            "hermes_cli.cron._active_cron_provider_name", lambda: "chronos"
        )
        monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda: [])
        cron_command(
            Namespace(
                cron_command="create",
                schedule="every 2m",
                prompt="Ping",
                name="Ping",
                deliver=None,
                repeat=None,
                skill=None,
                skills=None,
                script=None,
                workdir=None,
                no_agent=False,
            )
        )
        out = capsys.readouterr().out
        assert "Created job" in out
        assert "Gateway is not running" not in out


def test_cron_list_warns_when_gateway_not_running(monkeypatch, capsys):
    monkeypatch.setattr(
        "cron.jobs.list_jobs",
        lambda include_disabled=False: [
            {
                "id": "job-1",
                "name": "Nightly docs",
                "schedule_display": "every day",
                "state": "scheduled",
                "enabled": True,
                "next_run_at": "2026-06-01T00:00:00Z",
                "deliver": ["local"],
            }
        ],
    )
    monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda: [])
    monkeypatch.setattr(cron_cli, "_active_cron_provider_name", lambda: "builtin")

    cron_cli.cron_list()

    out = capsys.readouterr().out
    assert "Gateway is not running" in out
    assert "Nightly docs" in out


def test_cron_status_reports_running_gateway(monkeypatch, capsys):
    monkeypatch.setattr(cron_cli, "_active_cron_provider_name", lambda: "builtin")
    monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda: [1234, 5678])
    monkeypatch.setattr(
        "cron.jobs.list_jobs",
        lambda include_disabled=False: [
            {"next_run_at": "2026-06-01T00:00:00Z"},
            {"next_run_at": "2026-05-31T12:00:00Z"},
        ],
    )

    cron_cli.cron_status()

    out = capsys.readouterr().out
    assert "Gateway is running" in out
    assert "1234, 5678" in out
    assert "2 active job(s)" in out
    assert "2026-05-31T12:00:00Z" in out


def test_cron_tick_invokes_scheduler_tick_with_verbose(monkeypatch):
    calls = []
    monkeypatch.setattr("cron.scheduler.tick", lambda verbose=False: calls.append(verbose))

    cron_cli.cron_tick()

    assert calls == [True]


def test_cron_create_success_prints_job_details(monkeypatch, capsys):
    monkeypatch.setattr(
        cron_cli,
        "_cron_api",
        lambda **kwargs: {
            "success": True,
            "job_id": "job-1",
            "name": "Nightly docs",
            "schedule": "every day",
            "skills": ["docs"],
            "next_run_at": "2026-06-01T00:00:00Z",
            "job": {
                "script": "scripts/build_docs.py",
                "no_agent": True,
                "workdir": "/tmp/repo",
            },
        },
    )
    monkeypatch.setattr(cron_cli, "_warn_if_gateway_not_running", lambda: None)

    args = SimpleNamespace(
        schedule="every day",
        prompt="refresh docs",
        name="Nightly docs",
        deliver=None,
        repeat=None,
        skill="docs",
        skills=None,
        script="scripts/build_docs.py",
        workdir="/tmp/repo",
        no_agent=True,
    )

    rc = cron_cli.cron_create(args)

    out = capsys.readouterr().out
    assert rc == 0
    assert "Created job: job-1" in out
    assert "Skills: docs" in out
    assert "Script: scripts/build_docs.py" in out
    assert "Mode: no-agent" in out
    assert "Workdir: /tmp/repo" in out
    assert "Next run: 2026-06-01T00:00:00Z" in out


def test_cron_create_failure_returns_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(cron_cli, "_cron_api", lambda **kwargs: {"success": False, "error": "boom"})

    args = SimpleNamespace(
        schedule="every day",
        prompt="refresh docs",
        name=None,
        deliver=None,
        repeat=None,
        skill=None,
        skills=None,
        script=None,
        workdir=None,
        no_agent=False,
    )

    rc = cron_cli.cron_create(args)

    out = capsys.readouterr().out
    assert rc == 1
    assert "Failed to create job: boom" in out
