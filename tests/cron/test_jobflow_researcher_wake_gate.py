"""Wake gate for the JobFlow researcher cron.

The researcher runs 12 times a day on `30 1-23/2 * * *` and has processed
**nine RESEARCH_REQUESTs in its entire history**. Telemetry over 2.34 days:
34 runs, 177 model calls, 684k uncached input tokens — almost all of it
spent discovering an empty inbox.

Its cron prompt is purely inbox-driven ("Check mailbox/researcher/inbox for
RESEARCH_REQUEST messages. For each: ..."), with no periodic maintenance, so
an empty inbox means there is genuinely nothing for it to do.

Conservative by construction, same as the jaum gate: sleeping when there WAS
work strands a request until the next tick two hours later, while waking when
there was none costs exactly what today's behavior already costs. Every
ambiguous case — an unreadable entry, a missing directory, an unexpected error
— wakes the agent.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

# The deployed gate script, relative to the Hermes root. Note that `profiles/`
# is tracked in the *outer* Hermes repo (`~/.hermes`), not in `agent-src` — so
# the anchor is `agent-src`'s parent, not the `agent-src` repo root.
_GATE_REL = (
    pathlib.Path("profiles") / "main" / "scripts"
    / "jobflow_researcher_wake_gate.py"
)


def _find_hermes_root() -> pathlib.Path | None:
    """Return the Hermes root (the checkout holding `profiles/`), or None.

    Searches upward from this file for the directory that actually contains
    the gate script. Deliberately *not* a fixed count of `.parent` hops: this
    file sits three levels deeper inside a git worktree
    (`agent-src/.claude/worktrees/<name>/tests/cron/`) than in the main
    checkout (`agent-src/tests/cron/`), so any fixed count that escapes the
    checkout is right in exactly one of the two layouts. The previous
    `parents[3]` was correct only from the main checkout and silently resolved
    to `agent-src/.claude/worktrees` otherwise, failing all ten tests with a
    FileNotFoundError.

    Git cannot anchor this either: `rev-parse --show-toplevel` returns the
    *worktree* path rather than `agent-src`, and `hermes_constants`'
    `get_default_hermes_root()` reads `HERMES_HOME`, which the suite redirects
    to a per-test tempdir (see `tests/conftest.py`) — the very reason this path
    is hand-derived. Searching for the artifact itself is correct at any
    nesting depth and needs neither a subprocess nor the environment.
    """
    for candidate in pathlib.Path(__file__).resolve().parents:
        if (candidate / _GATE_REL).is_file():
            return candidate
    return None


_HERMES_ROOT = _find_hermes_root()
if _HERMES_ROOT is None:
    pytest.skip(
        f"Hermes root not found: no ancestor of {pathlib.Path(__file__).resolve()} "
        f"contains {_GATE_REL.as_posix()}. The gate script is deployed in the "
        "outer ~/.hermes checkout, which is absent from this tree.",
        allow_module_level=True,
    )

GATE = _HERMES_ROOT / _GATE_REL


def _load():
    spec = importlib.util.spec_from_file_location("researcher_gate", GATE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["researcher_gate"] = module
    spec.loader.exec_module(module)
    return module


def _decide(tmp_path, capsys, inbox_exists=True, files=()):
    inbox = tmp_path / "inbox"
    if inbox_exists:
        inbox.mkdir(parents=True, exist_ok=True)
        for name, body in files:
            (inbox / name).write_text(body, encoding="utf-8")
    module = _load()
    module.main(inbox)
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines, "the gate must always print something"
    try:
        gate = json.loads(lines[-1])
    except json.JSONDecodeError:
        return True, out  # non-JSON last line means wake, per _parse_wake_gate
    return gate.get("wakeAgent", True) is not False, out


class TestSleepsWhenThereIsNothingToDo:
    def test_an_empty_inbox_sleeps(self, tmp_path, capsys):
        wake, out = _decide(tmp_path, capsys)
        assert wake is False
        assert '"wakeAgent": false' in out.replace("'", '"')


class TestWakesWhenThereIsWork:
    def test_a_research_request_wakes(self, tmp_path, capsys):
        wake, _ = _decide(tmp_path, capsys, files=[
            ("20260813T0100_RESEARCH_REQUEST_main_ab.json", '{"type":"RESEARCH_REQUEST"}'),
        ])
        assert wake is True

    def test_several_requests_wake(self, tmp_path, capsys):
        wake, out = _decide(tmp_path, capsys, files=[
            ("a_RESEARCH_REQUEST.json", "{}"), ("b_RESEARCH_REQUEST.json", "{}"),
        ])
        assert wake is True
        assert "2" in out


class TestConservativeOnAmbiguity:
    """Anything this cannot positively classify as "no work" wakes."""

    def test_an_unrecognised_file_wakes(self, tmp_path, capsys):
        wake, _ = _decide(tmp_path, capsys, files=[("stray.txt", "hello")])
        assert wake is True

    def test_an_unparseable_json_file_wakes(self, tmp_path, capsys):
        wake, _ = _decide(tmp_path, capsys, files=[("broken.json", "{not json")])
        assert wake is True

    def test_a_missing_inbox_directory_wakes(self, tmp_path, capsys):
        """A vanished inbox is a misconfiguration a person should see."""
        wake, _ = _decide(tmp_path, capsys, inbox_exists=False)
        assert wake is True

    def test_an_unexpected_error_wakes(self, tmp_path, capsys, monkeypatch):
        """Arms the error path specifically.

        The inbox must EXIST here, or main() returns at the missing-directory
        branch and never reaches the listing — which is how an earlier version
        of this test passed while the error path was free to sleep.
        """
        inbox = tmp_path / "inbox"
        inbox.mkdir(parents=True)
        module = _load()

        def _boom(*a, **k):
            raise OSError("filesystem unavailable")

        monkeypatch.setattr(module, "_entries", _boom)
        module.main(inbox)
        lines = [x for x in capsys.readouterr().out.splitlines() if x.strip()]
        assert '"wakeAgent": false' not in lines[-1].replace("'", '"')


class TestOutputContract:
    def test_the_gate_line_is_last(self, tmp_path, capsys):
        _, out = _decide(tmp_path, capsys)
        lines = [x for x in out.splitlines() if x.strip()]
        assert json.loads(lines[-1]) == {"wakeAgent": False}

    def test_the_gate_never_raises(self, tmp_path, capsys):
        module = _load()
        module.main(tmp_path / "definitely" / "absent")  # must not raise

    def test_waking_output_has_no_false_gate_line(self, tmp_path, capsys):
        _, out = _decide(tmp_path, capsys, files=[("x_RESEARCH_REQUEST.json", "{}")])
        for line in out.splitlines():
            if line.strip().startswith("{"):
                assert json.loads(line).get("wakeAgent") is not False
