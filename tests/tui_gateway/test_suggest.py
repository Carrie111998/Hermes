"""Tests for the deterministic composer ghost-suggestion extractor (#slice-1).

The extractor sees ONLY the assistant's final message text and proposes at
most three likely user replies. It must never suggest when the message does
not ask anything, and must never treat commands the user was told to RUN
(fenced code) as reply material.
"""

from tui_gateway.suggest import Candidate, extract_suggestions


class TestNoSuggestion:
    def test_empty_text(self):
        assert extract_suggestions("") == []

    def test_whitespace_only(self):
        assert extract_suggestions("  \n \t ") == []

    def test_plain_prose_without_question(self):
        text = "Deployed the fix and all tests pass. The gateway restarted cleanly."
        assert extract_suggestions(text) == []

    def test_question_far_above_tail_does_not_fire(self):
        # A question 40 lines up followed by a long report should not
        # produce stale suggestions.
        text = "Want me to proceed?\n" + "\n".join(f"line {i}" for i in range(40))
        assert extract_suggestions(text) == []


class TestPathSuggestions:
    def test_backticked_path_in_trailing_question(self):
        text = (
            "Export finished. The report should be at "
            "`~/Downloads/BTC_Campaign_Engine_v0.1.5.xlsx` - is that the file you see?"
        )
        cands = extract_suggestions(text)
        assert cands[0] == Candidate(
            "~/Downloads/BTC_Campaign_Engine_v0.1.5.xlsx", "path"
        )

    def test_last_mentioned_path_ranks_first(self):
        text = (
            "I compared `/tmp/old.csv` against `~/Work/new.csv`.\n"
            "Which file should I keep?"
        )
        cands = extract_suggestions(text)
        assert [c.text for c in cands[:2]] == ["~/Work/new.csv", "/tmp/old.csv"]
        assert all(c.kind == "path" for c in cands[:2])

    def test_path_inside_code_fence_is_not_a_reply(self):
        # The user was told to RUN this; the path is not an answer.
        text = (
            "Run this and check the output:\n"
            "```bash\ncat /etc/hosts\n```\n"
            "Does the output look right?"
        )
        cands = extract_suggestions(text)
        assert all("/etc/hosts" not in c.text for c in cands)


class TestOptionSuggestions:
    def test_trailing_numbered_options_first_two(self):
        text = (
            "Three ways to handle the migration. Which approach?\n"
            "1. Fast path with a feature flag\n"
            "2. Safe path behind a worktree\n"
            "3. Skip it entirely"
        )
        cands = extract_suggestions(text)
        texts = [c.text for c in cands if c.kind == "option"]
        assert texts == ["Fast path with a feature flag", "Safe path behind a worktree"]

    def test_numbered_list_without_any_question_does_not_fire(self):
        text = "Changes made:\n1. Fixed the plist\n2. Added the marker"
        assert extract_suggestions(text) == []


class TestConfirmSuggestions:
    def test_want_me_to_yields_yes(self):
        text = "The fix is ready in the worktree. Want me to apply it to the live gateway?"
        cands = extract_suggestions(text)
        assert Candidate("Yes, go ahead", "confirm") in cands

    def test_look_right_yields_yes(self):
        text = "Here is the final shape of the config. Does that look right?"
        assert Candidate("Yes, go ahead", "confirm") in extract_suggestions(text)

    def test_open_question_is_not_confirm_shaped(self):
        text = "What should the timeout be?"
        cands = extract_suggestions(text)
        assert all(c.kind != "confirm" for c in cands)


class TestRankingAndLimits:
    def test_path_outranks_confirm_and_caps_at_three(self):
        text = (
            "I wrote `/a/one.txt`, `/a/two.txt` and `/a/three.txt` and `/a/four.txt`.\n"
            "Want me to keep going?"
        )
        cands = extract_suggestions(text)
        assert len(cands) == 3
        assert cands[0].kind == "path"

    def test_duplicate_paths_dedupe(self):
        text = "Saved to `~/x.md`. I mentioned `~/x.md` twice - should I open it?"
        cands = extract_suggestions(text)
        assert [c.text for c in cands if c.kind == "path"] == ["~/x.md"]
