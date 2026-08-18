"""Tests for `hermes overrides list|clear` — the visible/reversible safety
valve for Phase 2 model reroute overrides (spec Sec:Containment).

A Telegram tap can set an override from a phone; this CLI is the only way
to see and revoke one without one. Covers: friendly empty state, listing
with targets + expiry, single clear, "nothing matched" (not silent
success), --all, and that cleared_by reaches events.model_override.clear_override
so the audit trail can tell a CLI revoke apart from a Telegram one.
"""
from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


def _record(
    provider="anthropic",
    model="claude-sonnet-4-6",
    replacement_provider="openai",
    replacement_model="gpt-5.4",
    expires_in_seconds=3600,
    set_by="telegram:12345",
):
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)
    return {
        "provider": provider,
        "model": model,
        "replacement_provider": replacement_provider,
        "replacement_model": replacement_model,
        "expires_at": expires_at.isoformat(timespec="seconds"),
        "set_by": set_by,
        "set_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# cmd_overrides_list
# ---------------------------------------------------------------------------

class TestListCommand:
    def test_list_empty_is_friendly(self, capsys):
        from hermes_cli.overrides_cmd import cmd_overrides_list

        with patch("hermes_cli.overrides_cmd.list_overrides", return_value=[]):
            cmd_overrides_list(types.SimpleNamespace())

        out = capsys.readouterr().out
        assert "No active" in out or "no active" in out
        assert "Traceback" not in out

    def test_list_with_two_overrides_shows_both(self, capsys):
        from hermes_cli.overrides_cmd import cmd_overrides_list

        records = [
            _record(provider="anthropic", model="claude-sonnet-4-6",
                    replacement_provider="openai", replacement_model="gpt-5.4",
                    set_by="telegram:12345"),
            _record(provider="openai", model="gpt-5.4",
                    replacement_provider="nous", replacement_model="Hermes-4",
                    set_by="cli:diego"),
        ]
        with patch("hermes_cli.overrides_cmd.list_overrides", return_value=records):
            cmd_overrides_list(types.SimpleNamespace())

        out = capsys.readouterr().out
        # which model is being avoided, what it routes to instead
        assert "anthropic" in out and "claude-sonnet-4-6" in out
        assert "openai" in out and "gpt-5.4" in out
        assert "nous" in out and "Hermes-4" in out
        # who set it
        assert "telegram:12345" in out
        assert "cli:diego" in out
        # expiry shown as remaining time, not just a raw timestamp
        assert "expires in" in out

    def test_list_shows_remaining_time_not_just_raw_timestamp(self, capsys):
        from hermes_cli.overrides_cmd import cmd_overrides_list

        records = [_record(expires_in_seconds=4 * 3600 + 12 * 60)]
        with patch("hermes_cli.overrides_cmd.list_overrides", return_value=records):
            cmd_overrides_list(types.SimpleNamespace())

        out = capsys.readouterr().out
        assert "expires in 4h" in out


# ---------------------------------------------------------------------------
# cmd_overrides_clear
# ---------------------------------------------------------------------------

class TestClearCommand:
    def test_clear_one_by_provider_model_removes_only_that_one(self, capsys):
        from hermes_cli.overrides_cmd import cmd_overrides_clear

        with patch("hermes_cli.overrides_cmd.clear_override", return_value=True) as mock_clear:
            rc = cmd_overrides_clear(
                types.SimpleNamespace(provider="anthropic", model="claude-sonnet-4-6", all=False)
            )

        assert rc in (0, None)
        mock_clear.assert_called_once()
        _, kwargs = mock_clear.call_args
        assert kwargs["provider"] == "anthropic"
        assert kwargs["model"] == "claude-sonnet-4-6"
        out = capsys.readouterr().out
        assert "anthropic" in out
        assert "claude-sonnet-4-6" in out
        assert "Cleared" in out or "cleared" in out

    def test_clear_nonexistent_reports_nothing_matched_not_success(self, capsys):
        from hermes_cli.overrides_cmd import cmd_overrides_clear

        with patch("hermes_cli.overrides_cmd.clear_override", return_value=False):
            rc = cmd_overrides_clear(
                types.SimpleNamespace(provider="nope", model="nope-model", all=False)
            )

        out = capsys.readouterr().out
        assert "nothing matched" in out.lower() or "no override found" in out.lower() or \
            "no active override" in out.lower()
        # Must NOT claim success — either a truthy "cleared" word must be
        # absent, or the exit code must be non-zero.
        claims_cleared = "cleared" in out.lower() and "not cleared" not in out.lower()
        assert not claims_cleared or rc not in (0, None)

    def test_clear_all_clears_everything(self, capsys):
        from hermes_cli.overrides_cmd import cmd_overrides_clear

        records = [
            _record(provider="anthropic", model="claude-sonnet-4-6"),
            _record(provider="openai", model="gpt-5.4"),
        ]
        with patch("hermes_cli.overrides_cmd.list_overrides", return_value=records), \
             patch("hermes_cli.overrides_cmd.clear_override", return_value=True) as mock_clear:
            rc = cmd_overrides_clear(types.SimpleNamespace(provider=None, model=None, all=True))

        assert rc in (0, None)
        assert mock_clear.call_count == 2
        cleared_pairs = {
            (c.kwargs["provider"], c.kwargs["model"]) for c in mock_clear.call_args_list
        }
        assert cleared_pairs == {("anthropic", "claude-sonnet-4-6"), ("openai", "gpt-5.4")}
        out = capsys.readouterr().out
        assert "2" in out

    def test_clear_all_with_no_overrides_reports_nothing_to_clear(self, capsys):
        from hermes_cli.overrides_cmd import cmd_overrides_clear

        with patch("hermes_cli.overrides_cmd.list_overrides", return_value=[]):
            cmd_overrides_clear(types.SimpleNamespace(provider=None, model=None, all=True))

        out = capsys.readouterr().out
        assert "no" in out.lower()
        assert "Traceback" not in out

    def test_cleared_by_reaches_clear_override(self, capsys):
        """The audit trail needs to distinguish a CLI revoke from a Telegram
        one — cleared_by must be a non-empty, CLI-identifying string."""
        from hermes_cli.overrides_cmd import cmd_overrides_clear

        with patch("hermes_cli.overrides_cmd.clear_override", return_value=True) as mock_clear:
            cmd_overrides_clear(
                types.SimpleNamespace(provider="anthropic", model="claude-sonnet-4-6", all=False)
            )

        _, kwargs = mock_clear.call_args
        assert kwargs.get("cleared_by")
        assert "cli" in kwargs["cleared_by"].lower()

    def test_clear_all_passes_cleared_by_to_every_call(self, capsys):
        from hermes_cli.overrides_cmd import cmd_overrides_clear

        records = [_record(provider="anthropic", model="claude-sonnet-4-6")]
        with patch("hermes_cli.overrides_cmd.list_overrides", return_value=records), \
             patch("hermes_cli.overrides_cmd.clear_override", return_value=True) as mock_clear:
            cmd_overrides_clear(types.SimpleNamespace(provider=None, model=None, all=True))

        _, kwargs = mock_clear.call_args
        assert kwargs.get("cleared_by")
        assert "cli" in kwargs["cleared_by"].lower()

    def test_clear_without_provider_model_or_all_is_a_usage_error(self, capsys):
        from hermes_cli.overrides_cmd import cmd_overrides_clear

        rc = cmd_overrides_clear(types.SimpleNamespace(provider=None, model=None, all=False))
        assert rc not in (0, None)
        capsys.readouterr()


# ---------------------------------------------------------------------------
# cmd_overrides dispatcher
# ---------------------------------------------------------------------------

class TestDispatcher:
    def test_no_subcommand_lists(self, capsys):
        from hermes_cli.overrides_cmd import cmd_overrides

        with patch("hermes_cli.overrides_cmd.list_overrides", return_value=[]):
            cmd_overrides(types.SimpleNamespace(overrides_command=None))

        out = capsys.readouterr().out
        assert "No active" in out or "no active" in out

    def test_list_alias(self, capsys):
        from hermes_cli.overrides_cmd import cmd_overrides

        with patch("hermes_cli.overrides_cmd.list_overrides", return_value=[]):
            cmd_overrides(types.SimpleNamespace(overrides_command="ls"))

        out = capsys.readouterr().out
        assert "No active" in out or "no active" in out

    def test_unknown_subcommand_reports_and_exits_nonzero(self, capsys):
        from hermes_cli.overrides_cmd import cmd_overrides

        rc = cmd_overrides(types.SimpleNamespace(overrides_command="bogus"))
        assert rc not in (0, None)


# ---------------------------------------------------------------------------
# argparse wiring — verify `hermes overrides` is registered in main.py
# ---------------------------------------------------------------------------

class TestArgparseWiring:
    def test_overrides_help_lists_subcommands(self):
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "overrides", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        out = result.stdout + result.stderr
        assert "list" in out
        assert "clear" in out

    def test_overrides_list_end_to_end_empty_state(self, tmp_path, monkeypatch):
        import subprocess
        import sys

        env = dict(**__import__("os").environ)
        env["HERMES_HOME"] = str(tmp_path / ".hermes")
        (tmp_path / ".hermes").mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "overrides", "list"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        out = result.stdout + result.stderr
        assert "Traceback" not in out
        assert "No active" in out or "no active" in out


# ---------------------------------------------------------------------------
# I4: a corrupt override file must not read as "no overrides"
# ---------------------------------------------------------------------------

class TestUnreadableStore:
    """``list_overrides()`` fails open to [] for a corrupt/unreadable file --
    correct for routing, wrong for the one caller whose whole job is to
    REPORT state. Printing "No active model overrides." for a store that is
    in fact dark (reads blank, writes permanently skipped) means the feature
    is broken and nothing says so.
    """

    def test_list_says_so_when_the_store_is_unreadable(self, capsys):
        from hermes_cli.overrides_cmd import cmd_overrides_list

        with (
            patch("hermes_cli.overrides_cmd.list_overrides", return_value=[]),
            patch("hermes_cli.overrides_cmd.store_status",
                  return_value={"readable": False, "path": "C:/x/model_overrides.json"}),
        ):
            rc = cmd_overrides_list(types.SimpleNamespace())

        out = capsys.readouterr().out
        assert rc != 0, "an unreadable store is not a clean empty state"
        assert "could not be read" in out
        assert "model_overrides.json" in out
        assert "No active model overrides." not in out

    def test_list_empty_readable_store_is_still_the_friendly_message(self, capsys):
        """THE CENTRAL INVARIANT: a genuinely empty, readable store reports
        exactly as before."""
        from hermes_cli.overrides_cmd import cmd_overrides_list

        with (
            patch("hermes_cli.overrides_cmd.list_overrides", return_value=[]),
            patch("hermes_cli.overrides_cmd.store_status",
                  return_value={"readable": True, "path": "C:/x/model_overrides.json"}),
        ):
            rc = cmd_overrides_list(types.SimpleNamespace())

        out = capsys.readouterr().out
        assert rc == 0
        assert "No active model overrides." in out
        assert "could not be read" not in out

    def test_clear_distinguishes_unreadable_from_nothing_matched(self, capsys):
        """"Nothing matched" tells the operator there is nothing to revoke.
        For an unreadable store that is a lie: there may well be a live
        override on disk that simply cannot be read."""
        from hermes_cli.overrides_cmd import cmd_overrides_clear

        args = types.SimpleNamespace(all=False, provider="deepseek",
                                     model="deepseek-v4-pro")
        with (
            patch("hermes_cli.overrides_cmd.clear_override", return_value=False),
            patch("hermes_cli.overrides_cmd.store_status",
                  return_value={"readable": False, "path": "C:/x/model_overrides.json"}),
        ):
            rc = cmd_overrides_clear(args)

        out = capsys.readouterr().out
        assert rc != 0
        assert "could not be read" in out
        assert "Nothing matched" not in out

    def test_clear_all_says_so_when_the_store_is_unreadable(self, capsys):
        from hermes_cli.overrides_cmd import cmd_overrides_clear

        args = types.SimpleNamespace(all=True, provider=None, model=None)
        with (
            patch("hermes_cli.overrides_cmd.list_overrides", return_value=[]),
            patch("hermes_cli.overrides_cmd.store_status",
                  return_value={"readable": False, "path": "C:/x/model_overrides.json"}),
        ):
            rc = cmd_overrides_clear(args)

        out = capsys.readouterr().out
        assert rc != 0
        assert "could not be read" in out
        assert "nothing to clear" not in out.lower()
