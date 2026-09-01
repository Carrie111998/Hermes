"""Regression coverage for profile-scoped cron CLI diagnostics (#99579)."""

from unittest.mock import patch

import pytest


def test_cron_list_names_inspected_profile_when_empty(capsys):
    import hermes_cli.cron as cron_cli

    with (
        patch("cron.jobs.list_jobs", return_value=[]),
        patch("hermes_cli.profiles.get_active_profile_name", return_value="otto-projektleiter"),
    ):
        cron_cli.cron_list()

    output = capsys.readouterr().out
    assert "Profile: otto-projektleiter" in output
    assert "No scheduled jobs." in output


def test_cron_status_scopes_negative_gateway_claim_to_profile(capsys):
    import hermes_cli.cron as cron_cli

    with (
        patch("hermes_cli.profiles.get_active_profile_name", return_value="default"),
        patch("hermes_cli.cron._active_cron_provider_name", return_value="builtin"),
        patch("hermes_cli.gateway.find_gateway_pids", return_value=[]),
        patch("gateway.status.is_gateway_runtime_lock_active", return_value=False),
        patch("cron.jobs.list_jobs", return_value=[]),
    ):
        cron_cli.cron_status()

    output = capsys.readouterr().out
    assert "Profile: default" in output
    assert "Gateway is not running for profile 'default'" in output
    assert "Gateway is not running — cron jobs will NOT fire" not in output


def test_top_level_help_documents_profile_selector():
    from hermes_cli._parser import build_top_level_parser, top_level_value_flag_sets

    parser, _subparsers, _chat_parser = build_top_level_parser()
    required, optional = top_level_value_flag_sets()

    assert "--profile PROFILE" in parser.format_help()
    assert {"--profile", "-p"} <= required
    assert {"--profile", "-p"}.isdisjoint(optional)


def test_top_level_parser_rejects_unconsumed_profile_selector():
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, _chat_parser = build_top_level_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--profile", "bad:name"])

    assert exc_info.value.code == 2
