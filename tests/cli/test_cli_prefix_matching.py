"""Tests for slash command prefix matching in HermesCLI.process_command."""
from unittest.mock import MagicMock, patch
from cli import HermesCLI


def _make_cli():
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.config = {}
    cli_obj.console = MagicMock()
    cli_obj.agent = None
    cli_obj.conversation_history = []
    cli_obj.session_id = None
    cli_obj._pending_input = MagicMock()
    return cli_obj


class TestSlashCommandPrefixMatching:
    def test_space_after_slash_shows_focused_syntax_hint(self):
        """Whitespace after / is a syntax error, not an empty command prefix."""
        cli_obj = _make_cli()
        printed = []

        with patch.object(cli_obj, '_handle_background_command') as mock_background, \
             patch("cli._cprint", side_effect=printed.append):
            result = cli_obj.process_command("/ btw patch what means")

        assert result is True
        mock_background.assert_not_called()
        assert len(printed) == 1
        assert "remove" in printed[0].lower()
        assert "space" in printed[0].lower()
        assert "/btw patch what means" in printed[0]

    def test_bare_slash_points_to_help_without_enumerating_matches(self):
        """A bare / should give one useful next step instead of every match."""
        cli_obj = _make_cli()
        printed = []

        with patch("cli._cprint", side_effect=printed.append):
            result = cli_obj.process_command("/")

        assert result is True
        assert len(printed) == 1
        assert "/help" in printed[0]
        assert "Did you mean" not in printed[0]
        assert "/background" not in printed[0]

    def test_unique_prefix_dispatches_command(self):
        """/con should dispatch to /config when it uniquely matches."""
        cli_obj = _make_cli()
        with patch.object(cli_obj, 'show_config') as mock_config:
            cli_obj.process_command("/con")
        mock_config.assert_called_once()



    def test_ambiguous_prefix_shows_suggestions(self):
        """/re matches multiple commands — should show ambiguous message."""
        cli_obj = _make_cli()
        with patch("cli._cprint") as mock_cprint:
            cli_obj.process_command("/re")
            printed = " ".join(str(c) for c in mock_cprint.call_args_list)
        assert "Ambiguous" in printed or "Did you mean" in printed



    def test_skill_command_prefix_matches(self):
        """A prefix that uniquely matches a skill command should dispatch it."""
        cli_obj = _make_cli()
        fake_skill = {"/test-skill-xyz": {"name": "Test Skill", "description": "test"}}
        printed = []
        cli_obj.console.print = lambda *a, **kw: printed.append(str(a))

        import cli as cli_mod
        with patch.object(cli_mod, '_skill_commands', fake_skill):
            cli_obj.process_command("/test-skill-xy")

        # Should NOT show "Unknown command" — should have dispatched or attempted skill
        unknown = any("Unknown command" in p for p in printed)
        assert not unknown, f"Expected skill prefix to match, got: {printed}"

    def test_ambiguous_between_builtin_and_skill(self):
        """Ambiguous prefix spanning builtin + skill commands shows suggestions."""
        cli_obj = _make_cli()
        # /help-extra is a fake skill that shares /hel prefix with /help
        fake_skill = {"/help-extra": {"name": "Help Extra", "description": "test"}}

        import cli as cli_mod
        with patch.object(cli_mod, '_skill_commands', fake_skill),              patch.object(cli_obj, 'show_help') as mock_help:
            cli_obj.process_command("/help")

        # /help is an exact match so should work normally, not show ambiguous
        mock_help.assert_called_once()
        printed = " ".join(str(c) for c in cli_obj.console.print.call_args_list)
        assert "Ambiguous" not in printed

    def test_shortest_match_preferred_over_longer_skill(self):
        """/qui should dispatch to /quit (5 chars) not report ambiguous with /quint-pipeline (15 chars)."""
        cli_obj = _make_cli()
        fake_skill = {"/quint-pipeline": {"name": "Quint Pipeline", "description": "test"}}

        import cli as cli_mod
        with patch.object(cli_mod, '_skill_commands', fake_skill):
            # /quit is caught by the exact "/quit" branch → process_command returns False
            result = cli_obj.process_command("/qui")

        # Returns False because /quit was dispatched (exits chat loop)
        assert result is False
        printed = " ".join(str(c) for c in cli_obj.console.print.call_args_list)
        assert "Ambiguous" not in printed

    def test_tied_shortest_matches_still_ambiguous(self):
        """/re matches /reset and /retry (both 6 chars) — no unique shortest, stays ambiguous."""
        cli_obj = _make_cli()
        printed = []
        import cli as cli_mod
        with patch.object(cli_mod, '_cprint', side_effect=lambda t: printed.append(t)):
            cli_obj.process_command("/re")
        combined = " ".join(printed)
        assert "Ambiguous" in combined or "Did you mean" in combined

    def test_exact_typed_name_dispatches_over_longer_match(self):
        """/help typed with /help-extra skill installed → exact match wins."""
        cli_obj = _make_cli()
        fake_skill = {"/help-extra": {"name": "Help Extra", "description": ""}}
        import cli as cli_mod
        with patch.object(cli_mod, '_skill_commands', fake_skill), \
             patch.object(cli_obj, 'show_help') as mock_help:
            cli_obj.process_command("/help")
        mock_help.assert_called_once()
        printed = " ".join(str(c) for c in cli_obj.console.print.call_args_list)
        assert "Ambiguous" not in printed
