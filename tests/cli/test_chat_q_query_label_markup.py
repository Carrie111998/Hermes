"""Regression for #98789: query-file / -q text that looks like Rich markup.

The single-query path echoes a ``Query: <text>`` line before the agent turn.
The ``<text>`` half is user-controlled (``-q`` string or ``--query-file``
contents). It used to be interpolated into a markup-enabled ``console.print``,
so a pytest-style token like ``test_case[/fb-images/a/../b.webp?w=336]`` parsed
as an unmatched closing tag and raised ``rich.errors.MarkupError`` before the
run started. The label must render literally instead.
"""

import io
from types import SimpleNamespace

from rich.console import Console

import cli as cli_mod


# A path-like token with a bracketed segment that opens with '/', i.e. a Rich
# closing tag with no matching open tag — the exact shape from the bug report.
HOSTILE_LABEL = "test_case[/fb-images/a/../b.webp?w=336]"


def _run_single_query(monkeypatch, query):
    """Drive cli.main()'s human-facing single-query branch with a real Console.

    Returns the text written to the fake console.
    """
    buf = io.StringIO()

    class FakeCLI:
        def __init__(self, **_kwargs):
            self.console = Console(file=buf, force_terminal=False, width=200)
            self.session_id = "sq-markup-test"
            self.agent = SimpleNamespace(session_id="sq-markup-test", platform="cli")

        def _claim_active_session(self, surface, *, stderr=False):
            return True

        def _show_security_advisories(self):
            pass

        def chat(self, query, images=None):
            return "done"

        def _print_exit_summary(self, clear_screen=True):
            pass

    monkeypatch.setattr(cli_mod, "HermesCLI", FakeCLI)
    monkeypatch.setattr(cli_mod.atexit, "register", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli_mod, "_finalize_single_query", lambda _cli: None)

    cli_mod.main(query=query, quiet=False, toolsets="terminal")
    return buf.getvalue()


def test_bracketed_query_label_does_not_raise_markup_error(monkeypatch):
    # Without the escape this call raises rich.errors.MarkupError before the
    # agent turn; with it, main() completes and the label renders verbatim.
    out = _run_single_query(monkeypatch, HOSTILE_LABEL)
    assert "Query:" in out
    assert "test_case[/fb-images/a/../b.webp?w=336]" in out


def test_plain_query_label_still_renders(monkeypatch):
    out = _run_single_query(monkeypatch, "summarize the repo")
    assert "Query: summarize the repo" in out
