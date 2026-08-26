"""Skill-declared trigger phrases route free text to a skill's slash command.

The bug this closes: free text at the prompt is routed by the model, so a
skill invocation competes with every other skill for the model's attention.
A cheap model loses that race sometimes, and the failure is silent — a
plausible answer from the wrong skill rather than an error.
"""

import unittest
from unittest import mock

from agent.skill_commands import (
    _MAX_TRIGGER_LEN,
    _MAX_TRIGGERS,
    _normalize_triggers,
    match_skill_trigger,
)


def _cmds(mapping):
    """Build a minimal scan_skill_commands()-shaped map."""
    return {
        key: {"name": key.lstrip("/"), "description": "", "triggers": triggers}
        for key, triggers in mapping.items()
    }


class TestNormalizeTriggers(unittest.TestCase):
    def test_absent_yields_empty(self):
        self.assertEqual(_normalize_triggers({}), [])
        self.assertEqual(_normalize_triggers({"triggers": None}), [])

    def test_scalar_is_accepted_as_one_trigger(self):
        self.assertEqual(_normalize_triggers({"triggers": "list trips"}), ["list trips"])

    def test_lowercased_and_whitespace_collapsed(self):
        got = _normalize_triggers({"triggers": ["  List   Trips  "]})
        self.assertEqual(got, ["list trips"])

    def test_short_triggers_rejected(self):
        # "go" would capture an enormous amount of ordinary prose.
        got = _normalize_triggers({"triggers": ["go", "hi", "brief"]})
        self.assertEqual(got, ["brief"])

    def test_duplicates_collapsed(self):
        got = _normalize_triggers({"triggers": ["brief", "BRIEF", " brief "]})
        self.assertEqual(got, ["brief"])

    def test_non_string_entries_ignored(self):
        got = _normalize_triggers({"triggers": ["brief", 42, None, {"a": 1}]})
        self.assertEqual(got, ["brief"])

    def test_malformed_type_yields_empty(self):
        self.assertEqual(_normalize_triggers({"triggers": 42}), [])


class TestMatchSkillTrigger(unittest.TestCase):
    def setUp(self):
        self.commands = _cmds({"/trip-brief": ["brief", "list trips"]})

    def test_exact_trigger_matches(self):
        self.assertEqual(
            match_skill_trigger("list trips", self.commands),
            "/trip-brief list trips",
        )

    def test_trigger_with_trailing_text_matches(self):
        self.assertEqual(
            match_skill_trigger("brief the wine trip", self.commands),
            "/trip-brief brief the wine trip",
        )

    def test_case_and_spacing_insensitive(self):
        self.assertEqual(
            match_skill_trigger("  LIST   TRIPS  ", self.commands),
            "/trip-brief LIST   TRIPS",
        )

    def test_original_text_is_preserved_in_rewrite(self):
        # The skill receives what the user actually typed, not the normalized
        # form used for matching.
        out = match_skill_trigger("Brief The Wine Trip", self.commands)
        self.assertEqual(out, "/trip-brief Brief The Wine Trip")

    # --- the safety cases -------------------------------------------------

    def test_word_prefix_does_not_match(self):
        # "briefing" is not "brief"; hijacking it would be a regression.
        self.assertIsNone(match_skill_trigger("briefing notes on Q3", self.commands))

    def test_trigger_mid_sentence_does_not_match(self):
        # Only a leading trigger is an invocation. Otherwise any sentence
        # mentioning the word gets captured.
        self.assertIsNone(
            match_skill_trigger("can you brief me on the merger", self.commands)
        )

    def test_unrelated_text_untouched(self):
        self.assertIsNone(
            match_skill_trigger("what restaurants are near the hotel", self.commands)
        )

    def test_existing_slash_command_untouched(self):
        self.assertIsNone(match_skill_trigger("/help", self.commands))
        self.assertIsNone(match_skill_trigger("  /trip-brief 2", self.commands))

    def test_empty_and_non_string_input(self):
        self.assertIsNone(match_skill_trigger("", self.commands))
        self.assertIsNone(match_skill_trigger("   ", self.commands))
        self.assertIsNone(match_skill_trigger(None, self.commands))
        self.assertIsNone(match_skill_trigger(12345, self.commands))

    def test_no_skills_declare_triggers_is_inert(self):
        # The default state of every existing install.
        inert = _cmds({"/a": [], "/b": None})
        self.assertIsNone(match_skill_trigger("brief the wine trip", inert))

    def test_triggers_are_literal_not_regex(self):
        # A skill is untrusted input; a trigger must never compile as a
        # pattern or any skill could declare ".*" and capture everything.
        commands = _cmds({"/regexy": [".*", "a+b"]})
        self.assertIsNone(match_skill_trigger("anything at all", commands))
        self.assertEqual(
            match_skill_trigger("a+b something", commands), "/regexy a+b something"
        )

    # --- precedence -------------------------------------------------------

    def test_longest_trigger_wins(self):
        commands = _cmds({"/deploy": ["deploy"], "/deploy-staging": ["deploy staging"]})
        self.assertEqual(
            match_skill_trigger("deploy staging now", commands),
            "/deploy-staging deploy staging now",
        )
        self.assertEqual(
            match_skill_trigger("deploy prod now", commands),
            "/deploy deploy prod now",
        )

    def test_equal_length_ties_resolve_to_first_slug(self):
        # Scan order varies with the filesystem; the winner must not.
        a = _cmds({"/aaa": ["build"], "/zzz": ["build"]})
        b = _cmds({"/zzz": ["build"], "/aaa": ["build"]})
        self.assertEqual(match_skill_trigger("build it", a), "/aaa build it")
        self.assertEqual(match_skill_trigger("build it", b), "/aaa build it")

    def test_non_dict_command_entry_is_skipped(self):
        commands = {"/junk": "not a dict", "/ok": {"triggers": ["brief"]}}
        self.assertEqual(match_skill_trigger("brief x", commands), "/ok brief x")

    def test_malformed_command_entries_are_survivable(self):
        commands = {"/broken": {}, "/ok": {"triggers": ["brief"]}}
        self.assertEqual(match_skill_trigger("brief x", commands), "/ok brief x")


class TestTriggerQuotas(unittest.TestCase):
    """Frontmatter is untrusted, so a skill cannot declare unbounded triggers."""

    def test_phrase_count_is_capped(self):
        many = [f"trigger number {i}" for i in range(_MAX_TRIGGERS * 3)]
        got = _normalize_triggers({"triggers": many})
        self.assertEqual(len(got), _MAX_TRIGGERS)
        # The cap keeps the declared order rather than an arbitrary subset.
        self.assertEqual(got, [p.casefold() for p in many[:_MAX_TRIGGERS]])

    def test_over_long_phrase_is_dropped_not_truncated(self):
        long = "x" * (_MAX_TRIGGER_LEN + 1)
        got = _normalize_triggers({"triggers": [long, "brief"]})
        # Truncating would make the trigger match MORE than its author wrote.
        self.assertEqual(got, ["brief"])

    def test_phrase_at_the_limit_survives(self):
        exact = "y" * _MAX_TRIGGER_LEN
        self.assertEqual(_normalize_triggers({"triggers": [exact]}), [exact])

    def test_over_quota_skill_still_loads(self):
        # Malformed frontmatter degrades the feature; it must not raise.
        got = _normalize_triggers({"triggers": ["z" * 500] * 500})
        self.assertEqual(got, [])


class TestCaseFolding(unittest.TestCase):
    def test_non_ascii_trigger_matches_itself(self):
        # .lower() is lossy for some scripts; both sides must use the same fold
        # or a trigger stops matching the very phrase its author declared.
        triggers = _normalize_triggers({"triggers": ["STRASSE"]})
        commands = _cmds({"/street": triggers})
        self.assertIsNotNone(match_skill_trigger("strasse now", commands))

    def test_eszett_folds_consistently(self):
        triggers = _normalize_triggers({"triggers": ["straße"]})
        commands = _cmds({"/street": triggers})
        self.assertEqual(
            match_skill_trigger("STRASSE now", commands),
            "/street STRASSE now",
        )


class TestCLIWrapper(unittest.TestCase):
    """cli.match_skill_trigger() wires the matcher to the cached command scan.

    The wrapper is the never-gate boundary: whatever goes wrong underneath, the
    user's input has to reach the model unchanged.

    These exercise the wiring, not the matching -- the matcher itself is covered
    above against the real implementation. The wrapper resolves its impl through
    an ``import`` inside the function body, so driving the real matcher through
    it would make these tests depend on the module state every other test in the
    suite shares, and the wrapper's own ``except`` would swallow the evidence:
    an unrelated import problem elsewhere would surface here as a bare
    "None != '/trip-brief ...'" with no traceback to follow.
    """

    def _cli(self):
        import cli

        return cli

    def _impl(self, **kwargs):
        """Patch the impl the wrapper imports, so these test wiring only."""
        import agent.skill_commands

        return mock.patch.object(
            agent.skill_commands, "match_skill_trigger", **kwargs
        )

    def test_impl_result_is_returned_verbatim(self):
        cli = self._cli()
        commands = _cmds({"/trip-brief": ["brief"]})
        with mock.patch.object(cli, "get_skill_commands", return_value=commands):
            with self._impl(return_value="/trip-brief brief me") as impl:
                self.assertEqual(
                    cli.match_skill_trigger("brief me"), "/trip-brief brief me"
                )
        # The scanned command map has to reach the matcher; passing None would
        # make it rescan the filesystem on every submitted message.
        impl.assert_called_once_with("brief me", commands)

    def test_no_match_returns_none(self):
        cli = self._cli()
        with mock.patch.object(
            cli, "get_skill_commands", return_value=_cmds({"/trip-brief": ["brief"]})
        ):
            with self._impl(return_value=None):
                self.assertIsNone(cli.match_skill_trigger("what is the weather"))

    def test_matcher_failure_is_swallowed_too(self):
        # Not just a failing scan: the matcher itself must not be able to take
        # the prompt down.
        cli = self._cli()
        with mock.patch.object(cli, "_trigger_routing_failed", False):
            with mock.patch.object(cli, "get_skill_commands", return_value={}):
                with self._impl(side_effect=ValueError("boom")):
                    self.assertIsNone(cli.match_skill_trigger("brief me"))

    def test_real_matcher_reaches_the_wrapper(self):
        """One end-to-end pass with nothing stubbed but the scan.

        Asserts the wrapper did not silently fall into its except branch, so a
        genuine wiring break is reported as an error rather than a no-match.
        """
        cli = self._cli()
        with mock.patch.object(cli, "_trigger_routing_failed", False):
            with mock.patch.object(
                cli,
                "get_skill_commands",
                return_value=_cmds({"/trip-brief": ["brief"]}),
            ):
                with mock.patch.object(cli.logger, "warning") as warned:
                    got = cli.match_skill_trigger("brief me")
        if warned.called:
            self.fail(f"wrapper swallowed an exception: {warned.call_args}")
        self.assertEqual(got, "/trip-brief brief me")

    def test_scan_failure_is_swallowed_but_logged(self):
        cli = self._cli()
        with mock.patch.object(cli, "_trigger_routing_failed", False):
            with mock.patch.object(
                cli, "get_skill_commands", side_effect=RuntimeError("schema drift")
            ):
                with self.assertLogs(cli.logger, level="WARNING") as caught:
                    self.assertIsNone(cli.match_skill_trigger("brief me"))
        # Silence here is the actual bug: a broken scan would disable routing
        # everywhere and look exactly like "nothing matched".
        self.assertTrue(any("trigger routing" in m for m in caught.output))

    def test_repeat_failures_warn_only_once(self):
        cli = self._cli()
        with mock.patch.object(cli, "_trigger_routing_failed", False):
            with mock.patch.object(
                cli, "get_skill_commands", side_effect=RuntimeError("schema drift")
            ):
                with self.assertLogs(cli.logger, level="DEBUG") as caught:
                    for _ in range(5):
                        self.assertIsNone(cli.match_skill_trigger("brief me"))
        # A sticky failure must not warn on every message the user sends.
        warnings = [m for m in caught.output if m.startswith("WARNING")]
        self.assertEqual(len(warnings), 1)


if __name__ == "__main__":
    unittest.main()
