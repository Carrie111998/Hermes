"""Tests for `hermes update --yes / -y` — assume yes for interactive prompts.

Covers:
  1. argparse parses the flag
  2. Config-migration prompt is auto-answered (no input() call) and migrate_config
     runs with interactive=False so API-key prompts are skipped
"""

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from hermes_cli.main import cmd_update


def _make_run_side_effect(branch="main", verify_ok=True, commit_count="1"):
    """Minimal subprocess.run side_effect for the update flow."""

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)

        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{branch}\n", stderr="")
        if "rev-parse" in joined and "--verify" in joined:
            return subprocess.CompletedProcess(
                cmd, 0 if verify_ok else 128, stdout="", stderr=""
            )
        if "rev-list" in joined:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{commit_count}\n", stderr=""
            )
        # Clean local-work guard result.
        if "status" in joined and "--porcelain" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return side_effect


class TestUpdateYesConfigMigration:
    """--yes auto-answers the config-migration prompt and skips API-key prompts."""

    @patch("hermes_cli.config.migrate_config")
    @patch("hermes_cli.config.check_config_version", return_value=(1, 2))
    @patch("hermes_cli.config.get_missing_config_fields", return_value=[])
    @patch("hermes_cli.config.get_missing_env_vars", return_value=["NEW_KEY"])
    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_yes_auto_migrates_without_input(
        self,
        mock_run,
        _mock_which,
        _mock_missing_env,
        _mock_missing_cfg,
        _mock_version,
        mock_migrate,
        capsys,
    ):
        mock_run.side_effect = _make_run_side_effect(
            branch="main", verify_ok=True, commit_count="1"
        )
        mock_migrate.return_value = {"env_added": [], "config_added": []}

        args = SimpleNamespace(yes=True)

        with patch("builtins.input") as mock_input:
            cmd_update(args)
            # Never prompted the user.
            mock_input.assert_not_called()

        # migrate_config was invoked with interactive=False — API-key prompts
        # are suppressed, matching gateway-mode semantics.
        assert mock_migrate.call_count == 1
        _, kwargs = mock_migrate.call_args
        assert kwargs.get("interactive") is False

        out = capsys.readouterr().out
        assert "--yes: auto-applying config migration" in out
        # The "Would you like to configure them now?" prompt text never appears.
        assert "Would you like to configure them now?" not in out

    @patch("hermes_cli.config.migrate_config")
    @patch("hermes_cli.config.check_config_version", return_value=(1, 2))
    @patch("hermes_cli.config.get_missing_config_fields", return_value=[])
    @patch("hermes_cli.config.get_missing_env_vars", return_value=["NEW_KEY"])
    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_no_yes_flag_still_prompts_in_tty(
        self,
        mock_run,
        _mock_which,
        _mock_missing_env,
        _mock_missing_cfg,
        _mock_version,
        mock_migrate,
        capsys,
    ):
        """Regression guard: without --yes, the TTY prompt path still fires."""
        mock_run.side_effect = _make_run_side_effect(
            branch="main", verify_ok=True, commit_count="1"
        )
        mock_migrate.return_value = {"env_added": [], "config_added": []}

        args = SimpleNamespace(yes=False)

        # Patch ``sys.stdin.isatty`` and ``sys.stdout.isatty`` directly on the
        # real ``sys`` module instead of replacing ``hermes_cli.main.sys`` with
        # a MagicMock. The MagicMock approach was flaky under ``pytest-xdist``
        # — a sibling test that imported ``hermes_cli.main`` first could leave
        # a different ``sys`` reference resolved inside the function and the
        # mock would never be consulted, with CI then taking the
        # "Non-interactive session" branch instead of prompting.
        import sys as _sys

        with patch("builtins.input", return_value="n") as mock_input, patch.object(
            _sys.stdin, "isatty", return_value=True
        ), patch.object(_sys.stdout, "isatty", return_value=True):
            cmd_update(args)
            # The user was actually prompted.
            assert mock_input.called
            prompts = [c.args[0] if c.args else "" for c in mock_input.call_args_list]
            assert any("configure them now" in p for p in prompts)
