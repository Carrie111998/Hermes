"""Idle-tick stdout contract for the high-volume hybrid cron wrappers.

These jobs run every 15 minutes and their stored prompts do nothing but
reformat the script's own output and append ``[SILENT]`` on an idle tick --
i.e. the model call is a string formatter whose result is then suppressed.
A script that prints nothing on an idle tick makes ``_build_job_prompt``
return None, so the tick ends before a session, a model call, or a delivery.

Failures and real work must still reach stdout, because stdout is what gets
delivered.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from types import SimpleNamespace

import pytest

SCRIPTS = pathlib.Path.home() / ".hermes" / "profiles" / "main" / "scripts"


def _load(name):
    path = SCRIPTS / f"{name}.py"
    if not path.is_file():
        pytest.skip(f"script not present at {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    added = str(path.parent) not in sys.path
    if added:
        sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(mod)
    finally:
        if added:
            sys.path.remove(str(path.parent))
    return mod


def _iteration_json(out: str):
    """Extract the AGENT_ITERATION_JSON payload from stdout, if present."""
    if "<AGENT_ITERATION_JSON>" not in out:
        return None
    body = out.split("<AGENT_ITERATION_JSON>")[1].split("</AGENT_ITERATION_JSON>")[0]
    return json.loads(body)


def _stub_children(monkeypatch, mod, rc_sync=0, rc_export=0, out="", err="",
                   export_out=None, export_err=None):
    """Replace both child processes with a canned (returncode, stdout, stderr).

    ``export_out`` / ``export_err`` default to the bridge's, preserving the
    original single-payload behaviour. Pass them when a test counts lines: the
    two children are separate processes in production and do NOT echo each
    other, so feeding one payload to both would double every line.
    """
    calls = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        first = len(calls) == 1
        rc = rc_sync if first else rc_export
        return SimpleNamespace(
            returncode=rc,
            stdout=out if first or export_out is None else export_out,
            stderr=err if first or export_err is None else export_err,
        )

    monkeypatch.setattr(mod.subprocess, "run", _run)
    monkeypatch.setattr(mod.Path, "exists", lambda self: True)
    monkeypatch.setattr(
        mod,
        "fetch_alias_snapshot",
        lambda _directory: pathlib.Path("jobflow-alias-snapshot.json"),
    )
    return calls


class TestPostgresSync:
    @pytest.fixture
    def mod(self):
        return _load("postgres_sync")

    def _fake_run(self, monkeypatch, mod, rc_sync=0, rc_export=0, out="", err="",
                  export_out=None, export_err=None):
        return _stub_children(monkeypatch, mod, rc_sync, rc_export, out, err,
                              export_out, export_err)

    def test_successful_tick_is_silent(self, mod, monkeypatch, capsys):
        self._fake_run(monkeypatch, mod, out="synced 0 rows\n")

        assert mod.main() == 0
        assert capsys.readouterr().out == ""

    def test_sync_failure_still_alerts(self, mod, monkeypatch, capsys):
        self._fake_run(monkeypatch, mod, rc_sync=3, out="boom\n")

        assert mod.main() == 3
        out = capsys.readouterr().out
        assert "boom" in out
        payload = _iteration_json(out)
        assert payload is not None
        assert payload["counters"]["exit_code"] == 3
        assert payload["reason"] == "error"

    def test_export_failure_still_alerts(self, mod, monkeypatch, capsys):
        self._fake_run(monkeypatch, mod, rc_export=2)

        assert mod.main() == 2
        assert _iteration_json(capsys.readouterr().out)["counters"]["export_exit_code"] == 2

    def test_missing_target_still_fails_loudly(self, mod, monkeypatch, capsys):
        monkeypatch.setattr(mod.Path, "exists", lambda self: False)

        assert mod.main() == 1
        # Diagnostic goes to stderr; a non-zero rc is what surfaces the failure.
        assert "target script missing" in capsys.readouterr().err


class TestPostgresSyncWarningEscape:
    """A WARNING on an exit-0 tick must escape to stdout.

    The bridge's silent-rollback detector (hermes-postgres-bridge.py, commit
    5cf02a1a) prints "WARNING: business-state rollback:" naming each operator
    approval it is about to discard, and then exits 0 -- a rollback is not a
    lane failure. Before this contract that line was unreachable in production:
    the idle contract below routed ALL child output to stderr on exit 0, and
    ``agent-src/cron/scheduler.py:_run_job_script`` attaches stderr ONLY on a
    non-zero return code, discarding it on success. Measured 2026-08-24: all 50
    files in profiles/main/cron/output/9823bee8f270 were 0 bytes and the string
    "business-state" appeared nowhere in the gateway logs, whose window covered
    every post-commit run.

    A rollback WARNING is one-shot, not a repeating alarm: the discard writes
    EXCLUDED.current_business_state AND EXCLUDED.updated_at, so the row matches
    canonical afterwards and ``incoming_rank == existing_rank`` drops it from
    the diagnostics entirely on the next tick. Delivering it therefore cannot
    spam.
    """

    ROLLBACK = (
        "WARNING: business-state rollback: count=2 window_min=120 "
        "sample=k1:approved_for_tailor<-materials_ready truncated=0 "
        "-- canonical pipeline.json never caught up to these Postgres states; "
        "each is being overwritten now."
    )
    HELD = (
        "  business-state held: count=5 window_min=120 "
        "sample=k9:approved_for_tailor<-materials_ready truncated=0"
    )

    @pytest.fixture
    def mod(self):
        return _load("postgres_sync")

    def _fake_run(self, monkeypatch, mod, rc_sync=0, rc_export=0, out="", err="",
                  export_out=None, export_err=None):
        return _stub_children(monkeypatch, mod, rc_sync, rc_export, out, err,
                              export_out, export_err)

    def test_rollback_warning_reaches_stdout(self, mod, monkeypatch, capsys):
        self._fake_run(monkeypatch, mod, out=f"synced 10 rows\n{self.ROLLBACK}\n")

        assert mod.main() == 0
        assert self.ROLLBACK in capsys.readouterr().out

    def test_rollback_warning_tick_reports_itself_as_a_warning(
        self, mod, monkeypatch, capsys
    ):
        # Only the bridge emits the rollback line; the export child is quiet.
        self._fake_run(monkeypatch, mod, out=f"{self.ROLLBACK}\n", export_out="")

        assert mod.main() == 0
        payload = _iteration_json(capsys.readouterr().out)
        assert payload is not None, "a warning tick must carry an iteration summary"
        assert payload["reason"] == "warning"
        assert payload["counters"]["exit_code"] == 0
        assert payload["counters"]["warnings"] == 1

    def test_warning_arriving_on_child_stderr_also_escapes(
        self, mod, monkeypatch, capsys
    ):
        """The bridge prints to stdout, but never rely on which pipe carried it."""
        self._fake_run(monkeypatch, mod, err=f"{self.ROLLBACK}\n")

        assert mod.main() == 0
        assert self.ROLLBACK in capsys.readouterr().out

    def test_held_line_alone_keeps_the_tick_silent(self, mod, monkeypatch, capsys):
        """A healthy hold is the expected steady state -- it must stay quiet."""
        self._fake_run(monkeypatch, mod, out=f"{self.HELD}\n")

        assert mod.main() == 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "business-state held" in captured.err

    def test_non_warning_diagnostics_stay_off_stdout(self, mod, monkeypatch, capsys):
        self._fake_run(monkeypatch, mod, out=f"{self.HELD}\n{self.ROLLBACK}\n")

        assert mod.main() == 0
        captured = capsys.readouterr()
        assert "business-state held" not in captured.out
        assert "business-state held" in captured.err

    def test_warning_is_not_echoed_to_both_streams(self, mod, monkeypatch, capsys):
        """Duplicating it would double-report the same lost approval."""
        self._fake_run(monkeypatch, mod, out=f"{self.ROLLBACK}\n")

        assert mod.main() == 0
        captured = capsys.readouterr()
        assert self.ROLLBACK in captured.out
        assert "business-state rollback" not in captured.err

    def test_failure_path_is_unchanged_by_the_warning_contract(
        self, mod, monkeypatch, capsys
    ):
        """A real lane failure still wins: rc propagates and reason stays error."""
        self._fake_run(monkeypatch, mod, rc_sync=3, out=f"{self.ROLLBACK}\n")

        assert mod.main() == 3
        payload = _iteration_json(capsys.readouterr().out)
        assert payload["reason"] == "error"
        assert payload["counters"]["exit_code"] == 3


class TestJobflowApprovedRelease:
    @pytest.fixture
    def mod(self, tmp_path, monkeypatch):
        m = _load("jobflow_approved_release")
        canonical = tmp_path / "pipeline.json"
        canonical.write_text(json.dumps({"jobs": []}), encoding="utf-8")
        monkeypatch.setattr(m, "CANONICAL_PATH", canonical)
        monkeypatch.setattr(m, "already_requested_ids", lambda *args, **kwargs: set())
        monkeypatch.setattr(m, "mirror_approved_ids", lambda *args, **kwargs: set())
        monkeypatch.setattr(m, "research_pending_ids", lambda p: set())
        # argparse would otherwise consume pytest's own argv.
        monkeypatch.setattr("sys.argv", ["jobflow_approved_release.py"])
        return m

    def test_no_candidates_is_silent(self, mod, monkeypatch, capsys):
        monkeypatch.setattr(mod, "plan_releases", lambda *a, **k: [])

        assert mod.main() == 0
        assert capsys.readouterr().out == ""

    def test_all_skipped_stays_silent(self, mod, monkeypatch, capsys):
        """Matches the prompt's existing rule: silent when nothing was released."""
        monkeypatch.setattr(mod, "plan_releases", lambda *a, **k: [("j1", {}), ("j2", {})])
        monkeypatch.setattr(mod, "classify_release", lambda **k: "skip")

        assert mod.main() == 0
        assert capsys.readouterr().out == ""

    def test_release_produces_output(self, mod, monkeypatch, capsys):
        monkeypatch.setattr(mod, "plan_releases", lambda *a, **k: [("j1", {})])
        monkeypatch.setattr(mod, "classify_release", lambda **k: "research")
        monkeypatch.setattr(mod, "research_artifacts_exist", lambda j: False)
        monkeypatch.setattr(mod, "read_research_attempts", lambda j: 0)
        monkeypatch.setattr(mod, "build_research_request", lambda *a, **k: {"job_id": "j1"})
        monkeypatch.setattr(mod, "_write_research_request",
                            lambda msg, now: pathlib.Path("req.json"))

        assert mod.main() == 0
        out = capsys.readouterr().out
        assert "research=1" in out
        payload = _iteration_json(out)
        assert payload["counters"]["requested_research"] == 1

    def test_dry_run_always_reports(self, mod, monkeypatch, capsys):
        """A human ran it explicitly; never swallow their output."""
        monkeypatch.setattr(mod, "plan_releases", lambda *a, **k: [])
        monkeypatch.setattr("sys.argv", ["jobflow_approved_release.py", "--dry-run"])

        assert mod.main() == 0
        assert "dry-run" in capsys.readouterr().out
