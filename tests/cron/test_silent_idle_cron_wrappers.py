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


class TestPostgresSync:
    @pytest.fixture
    def mod(self):
        return _load("postgres_sync")

    def _fake_run(self, monkeypatch, mod, rc_sync=0, rc_export=0, out="", err=""):
        calls = []

        def _run(cmd, **kwargs):
            calls.append(cmd)
            rc = rc_sync if len(calls) == 1 else rc_export
            return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

        monkeypatch.setattr(mod.subprocess, "run", _run)
        monkeypatch.setattr(mod.Path, "exists", lambda self: True)
        monkeypatch.setattr(
            mod,
            "fetch_alias_snapshot",
            lambda _directory: pathlib.Path("jobflow-alias-snapshot.json"),
        )
        return calls

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
