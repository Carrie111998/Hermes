"""Regression tests for the macOS verified file-open front ladder (#95261).

On macOS an ``open`` from a tool call can succeed (exit 0, document loads)
while its window lands BEHIND the Hermes desktop window — macOS ignores
activation hand-off from a non-frontmost process. These tests pin the
contract of ``tools.macos_open_front``:

* every subprocess argv is built exactly as intended (mock runner),
* the raise ladder escalates in order and each rung is VERIFIED by a
  fresh ``frontmost`` observation — never by a rung's exit code,
* a focus race is reported honestly as its own outcome, never as success,
* nothing fires off-macOS, for non-``open`` commands, or for explicit
  ``-g``/``--background`` launches.
"""

import inspect

import pytest

from tools import macos_open_front as mof


class FakeRunner:
    """Records every argv and answers from a script.

    ``frontmost``  — list consumed left-to-right by frontmost checks; the
                     last value repeats when exhausted. A value may be an
                     Exception instance or ("rc", stdout) to force failures.
    ``results``    — dict keyed by a matcher string: "activate", "find",
                     "setfront", "reopen", "reissue" → RunResult/rc int.
    """

    def __init__(self, frontmost, results=None):
        self.script = list(frontmost)
        self.results = results or {}
        self.calls = []
        self.checks = 0

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        if self._is_check(argv):
            self.checks += 1
            if not self.script:
                return mof.RunResult(0, "")
            value = self.script.pop(0)
            if isinstance(value, Exception):
                raise value
            if isinstance(value, tuple):
                return mof.RunResult(value[0], value[1])
            return mof.RunResult(0, value)
        kind = self._kind(argv)
        outcome = self.results.get(kind, 0)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, int):
            return mof.RunResult(outcome, "")
        return outcome

    @staticmethod
    def _is_check(argv):
        return (
            argv[:2] == ["osascript", "-e"] and "frontmost" in argv[-1]
        )

    @staticmethod
    def _kind(argv):
        if argv[:2] == ["osascript", "-e"]:
            if "to reopen" in " ".join(argv[2:]):
                return "reopen"
            if "System Events" in " ".join(argv[2:]):
                return "check"
            return "activate"
        if argv[:2] == ["lsappinfo", "find"]:
            return "find"
        if argv[:2] == ["lsappinfo", "setfront"]:
            return "setfront"
        if argv[:1] == ["open"]:
            return "reissue"
        return "other"

    # -- inspection helpers -------------------------------------------------

    def calls_of(self, kind):
        return [c for c in self.calls if self._kind(c) == kind]

    def kinds(self):
        return [self._kind(c) for c in self.calls]


def run_annotate(command, runner, **kw):
    defaults = dict(platform="darwin", env_type="local", sleep=lambda s: None,
                    settle=0.0)
    defaults.update(kw)
    return mof.annotate_macos_open_success(command, run=runner, **defaults)


# ---------------------------------------------------------------------------
# Gate conditions: when the tier must stay completely silent.
# ---------------------------------------------------------------------------


def test_non_darwin_returns_none_without_runner_calls():
    runner = FakeRunner(["Preview"])
    assert run_annotate("open -a Preview f.pdf", runner, platform="linux") is None
    assert runner.calls == []


def test_non_local_env_returns_none_without_runner_calls():
    runner = FakeRunner(["Preview"])
    assert run_annotate(
        "open -a Preview f.pdf", runner, env_type="ssh"
    ) is None
    assert runner.calls == []


def test_non_open_command_never_touches_subprocess():
    runner = FakeRunner(["Preview"])
    assert run_annotate("cargo build --release && ./x", runner) is None
    assert runner.calls == []


def test_explicit_background_launch_is_respected():
    runner = FakeRunner(["Hermes"])
    # `-g` asked for background: nothing to verify, no ladder.
    assert run_annotate("open -g -a Preview f.pdf", runner) is None
    assert runner.calls == []


def test_word_open_in_other_command_is_ignored():
    runner = FakeRunner(["Preview"])
    assert run_annotate("echo 'cannot open file' > err.txt", runner) is None
    assert runner.calls == []


# ---------------------------------------------------------------------------
# Parsing: locating the open invocation.
# ---------------------------------------------------------------------------


def test_parse_bare_open_has_no_app():
    inv = mof.parse_open_invocation("open report.pdf")
    assert inv is not None
    assert inv.argv == ["open", "report.pdf"]
    assert inv.app is None


def test_parse_extracts_app_from_short_and_long_flag():
    assert mof.parse_open_invocation("open -a Preview f.pdf").app == "Preview"
    assert mof.parse_open_invocation(
        'open --apps "Google Chrome" https://x.dev'
    ).app == "Google Chrome"


def test_parse_compound_command_with_quoted_path():
    inv = mof.parse_open_invocation('cd "/a b/c" && open -a Preview "f g.pdf"')
    assert inv is not None
    assert inv.argv == ["open", "-a", "Preview", "f g.pdf"]


def test_parse_env_prefix_and_full_path_prog():
    inv = mof.parse_open_invocation("FOO=1 /usr/bin/open x.pdf")
    assert inv is not None
    # The invocation is kept verbatim so the re-issue rung replays exactly
    # what ran; matching elsewhere uses the basename.
    assert inv.argv == ["/usr/bin/open", "x.pdf"]


def test_parse_last_open_wins():
    inv = mof.parse_open_invocation("open a.pdf && open b.pdf")
    assert inv.argv == ["open", "b.pdf"]


def test_parse_trims_background_and_redirect_tokens():
    assert mof.parse_open_invocation("open x.pdf & echo done").argv == [
        "open",
        "x.pdf",
    ]
    assert mof.parse_open_invocation("open x.pdf > /dev/null").argv == [
        "open",
        "x.pdf",
    ]


def test_reveal_flag_targets_finder():
    runner = FakeRunner(["Finder"])
    note = run_annotate("open -R /tmp/x.png", runner)
    assert note is not None
    assert "Finder" in note
    assert runner.calls_of("activate") == []


# ---------------------------------------------------------------------------
# The happy path: already visible.
# ---------------------------------------------------------------------------


def test_already_frontmost_reports_observation_without_ladder():
    runner = FakeRunner(["Preview"])
    note = run_annotate("open -a Preview f.pdf", runner)
    assert note is not None
    assert "frontmost='Preview'" in note
    assert runner.kinds() == ["check"]
    assert runner.checks == 1


def test_bundle_id_app_matches_process_name():
    runner = FakeRunner(["Preview"])
    note = run_annotate("open -a com.apple.Preview f.pdf", runner)
    assert note is not None
    assert runner.kinds() == ["check"]


# ---------------------------------------------------------------------------
# The swallowed-activation path: ladder order + exact argv under test.
# ---------------------------------------------------------------------------


def test_activate_rung_argv_exact_when_it_works():
    runner = FakeRunner(["Hermes", "Preview", "Preview"])
    note = run_annotate("open -a Preview f.pdf", runner)

    assert note is not None
    assert "behind Hermes" in note
    assert "confirmed frontmost='Preview'" in note

    # Exactly one rung was needed, with the exact AppleScript activation:
    assert runner.calls_of("activate") == [
        [
            "osascript",
            "-e",
            'tell application "Preview" to activate',
        ]
    ]
    # check → activate → verify-check → confirm-check
    assert runner.kinds() == ["check", "activate", "check", "check"]


def test_full_ladder_order_and_arguments():
    asn_value = "0x0-0x42 Preview"
    runner = FakeRunner(
        # checks: Hermes behind; every rung's observation until reopen;
        # reopen lands Preview; confirmation repeats it.
        ["Hermes", "Hermes", "Hermes", "Google Chrome", "Preview", "Preview"],
        results={
            "find": mof.RunResult(0, f'"ASN"="{asn_value}"'),
            "setfront": 1,  # rc deliberately ignored (#95261)
        },
    )
    note = run_annotate("open -a Preview f.pdf", runner)

    assert note is not None
    assert "confirmed frontmost='Preview'" in note

    # Escalation order per #95261: activate → lsappinfo find+setfront →
    # re-issue the ORIGINAL open argv verbatim → reopen+activate.
    assert runner.kinds() == [
        "check",
        "activate",
        "check",
        "find",
        "setfront",
        "check",
        "reissue",
        "check",
        "reopen",
        "check",
        "check",
    ]
    assert runner.calls_of("find") == [
        ["lsappinfo", "find", "-only", "asn", "LSDisplayName=Preview"]
    ]
    assert runner.calls_of("setfront") == [["lsappinfo", "setfront", asn_value]]
    assert runner.calls_of("reissue") == [["open", "-a", "Preview", "f.pdf"]]
    assert runner.calls_of("reopen") == [
        [
            "osascript",
            "-e",
            'tell application "Preview" to reopen',
            "-e",
            'tell application "Preview" to activate',
        ]
    ]


def test_verdict_comes_from_observation_not_exit_codes():
    # activate exits non-zero but Preview IS observed frontmost afterwards:
    # the observation wins (#95261: never trust a rung's return code).
    runner = FakeRunner(
        ["Hermes", "Preview", "Preview"], results={"activate": 173}
    )
    note = run_annotate("open -a Preview f.pdf", runner)
    assert note is not None
    assert "confirmed frontmost='Preview'" in note
    # No further escalation after the observation said we're done:
    assert runner.calls_of("find") == []


def test_focus_race_is_reported_honestly_as_failure():
    # Every rung runs, but Chrome keeps stealing focus — the final observed
    # state must NOT read as success (#95261's false-pass requirement).
    runner = FakeRunner(
        ["Hermes"] + ["Google Chrome"] * 8,
        results={"find": mof.RunResult(0, '"ASN"="0x0-0x42 Preview"')},
    )
    note = run_annotate("open -a Preview f.pdf", runner)

    assert note is not None
    assert "NOT IN FRONT" in note
    assert "'Google Chrome' holds focus" in note
    # No success-shaped phrasing may survive into a focus-race note:
    assert "safe to report" not in note
    assert "is frontmost" not in note
    assert len(runner.calls_of("check")) >= 5


def test_unverified_when_checks_die_mid_ladder():
    # First check works (Hermes is behind), then the frontmost query starts
    # failing after the activate rung — honest "could not confirm".
    runner = FakeRunner(["Hermes", (1, "")])
    note = run_annotate("open -a Preview f.pdf", runner)
    assert note is not None
    assert "verification stopped answering" in note
    assert "last observed frontmost='Hermes'" in note


def test_initial_check_failure_stays_silent():
    # osascript unavailable at all: no annotation noise on headless boxes.
    runner = FakeRunner([(1, "")])
    assert run_annotate("open -a Preview f.pdf", runner) is None
    assert runner.kinds() == ["check"]


# ---------------------------------------------------------------------------
# Bare opens (no -a): target unknown until observed.
# ---------------------------------------------------------------------------


def test_bare_open_reissue_activates_document_app():
    runner = FakeRunner(["Hermes", "Preview", "Preview"])
    note = run_annotate("open f.pdf", runner)
    assert note is not None
    assert "confirmed frontmost='Preview'" in note
    # Only the re-issue rung exists for bare opens, passing the original
    # argv through verbatim:
    assert runner.kinds() == ["check", "reissue", "check", "check"]
    assert runner.calls_of("reissue") == [["open", "f.pdf"]]


def test_bare_open_left_behind_reports_distinct_failure():
    runner = FakeRunner(["Hermes", "Hermes"])
    note = run_annotate("open f.pdf", runner)
    assert note is not None
    assert "NOT IN FRONT" in note
    assert "'the document' loaded" in note


def test_bare_open_with_other_app_in_front_counts_as_visible():
    # Focus already moved off Hermes onto something else — treat as seen.
    runner = FakeRunner(["Safari"])
    note = run_annotate("open page.html", runner)
    assert note is not None
    assert "frontmost='Safari'" in note
    assert runner.kinds() == ["check"]


# ---------------------------------------------------------------------------
# Argument hygiene.
# ---------------------------------------------------------------------------


def test_applescript_quotes_are_escaped():
    argv = mof.build_activate_argv('My "Great" App')
    assert argv == [
        "osascript",
        "-e",
        'tell application "My \\"Great\\" App" to activate',
    ]


def test_asn_parsing_handles_keyed_and_loose_forms():
    assert mof.parse_asn('"ASN"="0x0-0x42 Preview"') == "0x0-0x42 Preview"
    assert mof.parse_asn("0x0-0x42 Preview") == "0x0-0x42"
    assert mof.parse_asn("") is None
    assert mof.parse_asn("no asn here") is None


def test_settle_sleep_runs_between_actions_and_checks():
    sleeps = []
    runner = FakeRunner(["Hermes", "Preview", "Preview"])
    mof.annotate_macos_open_success(
        "open -a Preview f.pdf",
        platform="darwin",
        run=runner,
        sleep=sleeps.append,
        settle=0.5,
    )
    assert sleeps, "expected settle sleeps before each observation"
    assert all(s == 0.5 for s in sleeps)


# ---------------------------------------------------------------------------
# Wiring regression: the terminal tool must consult this tier on exit 0.
# ---------------------------------------------------------------------------


def test_terminal_tool_wires_the_tier_for_local_success_results():
    from tools import terminal_tool

    source = inspect.getsource(terminal_tool)
    assert "annotate_macos_open_success" in source
    # Gated on local execution so remote/container sessions are untouched:
    assert "env_type=env_type" in source


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
