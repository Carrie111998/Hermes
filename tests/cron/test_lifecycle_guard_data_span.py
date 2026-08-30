"""lifecycle_guard must not false-positive across a large single-line data file.

"kill" is a common English word; on a compact single-line JSON an unbounded gap
between the kill / gateway / hermes tokens spanned ~650 KB of unrelated records
and falsely blocked plain data processing (#98869). Branch D's gaps are now
bounded ({0,200}), so a far-apart coincidence no longer matches while a real
`pkill … gateway … hermes` command still does.
"""
from cron.lifecycle_guard import _GATEWAY_LIFECYCLE_PATTERN as P


def test_far_apart_tokens_on_one_line_do_not_match():
    # Standalone "kill", "gateway", "hermes" ~300 KB apart on one line — the
    # shape a compact (non-indented) JSON data file produces.
    blob = "junk kill " + "y" * 300_000 + " gateway " + "z" * 300_000 + " hermes junk"
    assert P.search(blob) is None


def test_real_pkill_gateway_command_still_matches():
    assert P.search("pkill -f 'hermes gateway'") is not None
    # Both token orders, and a verbose real invocation, stay caught.
    assert P.search("kill $(pgrep -f 'gateway') # hermes") is not None
    assert P.search(
        'sudo pkill -9 -f "python -m hermes_cli.main --profile default gateway run" hermes'
    ) is not None


def test_kill_suffix_word_still_does_not_match():
    # Regression guard for the earlier "skill" -> "kill" fix (leading \b).
    assert P.search("your next skill " + "y" * 40 + " gateway " + "z" * 40 + " hermes") is None
