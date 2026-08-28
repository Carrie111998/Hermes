"""The canonical assurance statement, in every location that exists (commit 12).

The specification calls for the statement to be reproduced verbatim in six
places. "Verbatim" cannot mean byte-identical across a Python docstring, a SQL
comment, a markdown blockquote and a test docstring — each medium sets its own
wrapping and prefix. It means word-for-word, and that is what these tests
enforce: normalise the prefixes and the whitespace away, then require an exact
match against ``approval_broker.ASSURANCE_STATEMENT``.

Two of the six locations do not exist yet — there is no desktop approval dialog
and no ``hermes project doctor``. Rather than quietly dropping them, the tests
below pin their absence, so the day either surface is built this file fails and
whoever builds it has to carry the statement along.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli.approval_broker import ASSURANCE_STATEMENT

REPO = Path(__file__).resolve().parents[2]

# Every prefix a medium adds in front of the words themselves.
_PREFIX = re.compile(r"^\s*(?:--|>|#|\*)\s?", re.MULTILINE)


def _words(text: str) -> str:
    """The statement's words, free of wrapping, prefixes and emphasis."""
    return " ".join(_PREFIX.sub("", text).replace("**", "").split())


CANON = _words(ASSURANCE_STATEMENT)


def _extract(haystack: str) -> str:
    """The canonical run of words, located by its first and last sentence."""
    start = haystack.index("Hermes approval gates are an integrity control")
    end = haystack.index("None of these is in Phase 1.", start)
    return _words(haystack[start:end + len("None of these is in Phase 1.")])


# --- the four locations that exist -----------------------------------------

def test_the_broker_module_docstring_carries_it():
    import hermes_cli.approval_broker as broker

    assert _extract(broker.__doc__) == CANON


def test_the_pm_approvals_schema_comment_carries_it():
    from hermes_cli.kanban_db import SCHEMA_SQL

    head = SCHEMA_SQL[:SCHEMA_SQL.index("CREATE TABLE IF NOT EXISTS pm_approvals")]
    assert _extract(head) == CANON


def test_the_pm_doc_carries_it():
    assert _extract((REPO / "docs" / "pm.md").read_text()) == CANON


def test_the_broker_test_docstring_carries_it():
    doc = (REPO / "tests" / "hermes_cli" / "test_approval_broker.py").read_text()
    assert _extract(doc[:doc.index('"""', 3)]) == CANON


# --- the statement is one claim, not five that drift ------------------------

def test_no_location_paraphrases_the_guarantee():
    """Each location must match exactly; a near-miss is the failure mode."""
    for text in (
        __import__("hermes_cli.approval_broker", fromlist=["x"]).__doc__,
        __import__("hermes_cli.kanban_db", fromlist=["x"]).SCHEMA_SQL,
        (REPO / "docs" / "pm.md").read_text(),
        (REPO / "tests" / "hermes_cli" / "test_approval_broker.py").read_text(),
    ):
        assert _extract(text) == CANON


@pytest.mark.parametrize("claim", [
    "integrity control, not a security boundary",
    "accidental self-approval",
    "prompt injection that emits an approval command",
    "replay of a previous approval",
    "do not provide a cryptographic boundary",
    "None of these is in Phase 1.",
])
def test_the_load_bearing_claims_survive_normalisation(claim):
    assert " ".join(claim.replace("**", "").split()) in CANON


# --- the SQL comment must not disturb the stored schema --------------------

def test_the_schema_comment_does_not_change_what_sqlite_stores(tmp_path, monkeypatch):
    """It sits above the CREATE, so `sqlite_master` and drift are untouched."""
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'pm_approvals'").fetchone()["sql"]
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(pm_approvals)")]
    finally:
        conn.close()
    assert "integrity control" not in sql, "the comment must stay out of the DDL"
    assert "subject" in cols and "binding_hash" in cols


# --- the two locations that do not exist yet -------------------------------

def test_there_is_still_no_desktop_approval_dialog():
    """When one is built, it must carry the statement — see this file."""
    desktop = REPO / "apps" / "desktop"
    if not desktop.is_dir():
        pytest.skip("no desktop app in this checkout")
    hits = subprocess.run(
        ["grep", "-rl", "awaiting_approval", str(desktop)],
        capture_output=True, text=True).stdout.split()
    assert not hits, (
        "a desktop approval surface now exists — add the assurance statement "
        f"to it and to this test: {hits}")


def test_there_is_still_no_project_doctor_verb():
    """`doctor` was deferred from commit 9; its header is location six."""
    import argparse

    from hermes_cli import projects_cmd

    root = argparse.ArgumentParser()
    projects_cmd.build_parser(root.add_subparsers())
    verbs = set()
    for action in root._subparsers._group_actions:
        for name, parser in action.choices.items():
            if name != "project":
                continue
            for sub in parser._subparsers._group_actions:
                verbs |= set(sub.choices)
    assert verbs, "the project verb list must be discoverable"
    assert "doctor" not in verbs, (
        "`hermes project doctor` now exists — its header is the sixth "
        f"assurance location, and this test is the reminder: {sorted(verbs)}")
