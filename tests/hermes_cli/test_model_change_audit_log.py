"""A global model/provider change that trips the cron drift guard must leave
a durable, searchable trail in agent.log — not just a print() to whatever
terminal happened to be attached (#44585 follow-up, NICHE-BOTS-T incident).

Before this fix, ``warn_unpinned_cron_jobs_after_model_config_change`` only
called ``print()``. A change made non-interactively, via a session no one
was watching, or whose terminal output was lost left zero trace — which is
what turned a routine model.default bump into a multi-hour incident
investigation with no way to pinpoint who/when changed it.
"""

import logging
from unittest.mock import MagicMock

import pytest

from hermes_cli.config import warn_unpinned_cron_jobs_after_model_config_change


def _unpinned_job(**overrides):
    job = {
        "id": "job-1",
        "name": "Nightly digest",
        "enabled": True,
        "no_agent": False,
        "provider_snapshot": "openrouter",
        "model_snapshot": "claude-sonnet-5",
    }
    job.update(overrides)
    return job


@pytest.fixture
def one_affected_job(monkeypatch):
    """Patch the job loader so build_cron_model_impact sees one drifted job."""
    monkeypatch.setattr(
        "hermes_cli.config._load_cron_jobs_for_config_warning",
        lambda: [_unpinned_job()],
    )


class TestModelChangeAuditLog:
    def test_logs_warning_with_old_and_new_model(self, caplog, one_affected_job):
        with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
            warn_unpinned_cron_jobs_after_model_config_change(
                "model.default",
                "claude-sonnet-4-6",
                config={},
                old_value="claude-sonnet-5",
            )

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "expected a logger.warning() audit-trail record"
        message = warnings[0].getMessage()
        assert "claude-sonnet-5" in message
        assert "claude-sonnet-4-6" in message
        assert "1" in message  # affected_count

    def test_still_prints_for_interactive_feedback(self, capsys, one_affected_job):
        warn_unpinned_cron_jobs_after_model_config_change(
            "model.default",
            "claude-sonnet-4-6",
            config={},
            old_value="claude-sonnet-5",
        )
        out = capsys.readouterr().out
        assert "unpinned cron" in out

    def test_no_log_when_no_jobs_affected(self, caplog, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config._load_cron_jobs_for_config_warning",
            lambda: [],
        )
        with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
            warn_unpinned_cron_jobs_after_model_config_change(
                "model.default",
                "claude-sonnet-4-6",
                config={},
                old_value="claude-sonnet-5",
            )
        assert not any(r.levelno == logging.WARNING for r in caplog.records)

    def test_old_value_optional_defaults_to_unknown_marker(self, caplog, one_affected_job):
        """A caller that cannot cheaply capture the pre-write value (or there
        genuinely wasn't one, e.g. first-ever write) must not crash or skip
        the log — it just can't name the old value."""
        with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
            warn_unpinned_cron_jobs_after_model_config_change(
                "model.default",
                "claude-sonnet-4-6",
                config={},
            )
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings
        assert "claude-sonnet-4-6" in warnings[0].getMessage()


class TestSaveConfigValueCapturesOldValue:
    """cli.py's save_config_value (the /model --global + TUI persist path)
    must snapshot the pre-write value and forward it, so the audit log names
    both sides of the change instead of just the new one."""

    def test_forwards_old_value_from_existing_config(self, tmp_path, monkeypatch):
        import yaml

        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            yaml.dump({"model": {"default": "claude-sonnet-5"}})
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setattr("cli._hermes_home", hermes_home)

        warning = MagicMock()
        monkeypatch.setattr(
            "hermes_cli.config.warn_unpinned_cron_jobs_after_model_config_change",
            warning,
        )

        from cli import save_config_value

        assert save_config_value("model.default", "claude-sonnet-4-6") is True
        warning.assert_called_once_with(
            "model.default", "claude-sonnet-4-6", old_value="claude-sonnet-5"
        )
