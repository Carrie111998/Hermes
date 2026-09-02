"""Single-query label must render literally, not as Rich markup (#98789).

``-q`` / ``--query-file`` text is user-controlled (the Bot Mode DM transport
feeds arbitrary handoff prose through it). The human single-query branch
prints it as ``[bold blue]Query:[/] {_query_label}`` via ``Console.print``,
which parses the string as Rich markup — an unmatched closing tag such as a
pytest id ``test_case[/fb-images/a/../b.webp?w=336]`` raised
``rich.errors.MarkupError`` before the agent turn even started.

These tests drive ``cli.main()`` through the same FakeCLI harness as
``test_single_query_session_finalize.py`` but give the fake console a *real*
``rich.console.Console`` so the markup parser actually runs.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from rich.console import Console

import cli as cli_mod

# The exact reproduction token from the issue: Rich sees ``[/fb-images/...]``
# as an unmatched closing tag.
_MARKUP_BREAKING_QUERY = "test_case[/fb-images/a/../b.webp?w=336]"


@pytest.fixture
def fake_cli_factory():
    calls = []
    stdout = io.StringIO()

    class FakeCLI:
        def __init__(self, **_kwargs):
            self.console = Console(
                file=stdout, width=80, no_color=True, highlight=False
            )
            self.session_id = "single-query-session"
            self.agent = SimpleNamespace(
                session_id="single-query-session",
                platform="cli",
            )

        def _claim_active_session(self, surface, *, stderr=False):
            calls.append(("claim", surface, stderr))
            return True

        def _show_security_advisories(self):
            calls.append("advisories")

        def chat(self, query, images=None):
            calls.append(("chat", query, images))
            return "done"

        def _print_exit_summary(self, clear_screen=True):
            calls.append("summary")

    return calls, stdout, FakeCLI


def _run_single_query(monkeypatch, fake_cli_factory, query):
    calls, stdout, fake_cls = fake_cli_factory
    monkeypatch.setattr(cli_mod, "HermesCLI", fake_cls)
    monkeypatch.setattr(cli_mod.atexit, "register", lambda *_a, **_k: None)
    monkeypatch.setattr(
        cli_mod,
        "_finalize_single_query",
        lambda fake_cli: calls.append(("finalize", fake_cli.session_id)),
    )

    cli_mod.main(query=query, quiet=False, toolsets="terminal")

    assert calls == [
        ("claim", "cli", False),
        "advisories",
        ("chat", query, None),
        "summary",
        ("finalize", "single-query-session"),
    ]
    return stdout.getvalue()


def test_single_query_label_with_unmatched_closing_tag_reaches_chat(
    monkeypatch, fake_cli_factory
):
    # Before the fix this raises MarkupError inside Console.print, so the
    # agent turn never starts; after it the run completes and the label is
    # rendered literally.
    output = _run_single_query(monkeypatch, fake_cli_factory, _MARKUP_BREAKING_QUERY)

    assert _MARKUP_BREAKING_QUERY in output


def test_single_query_label_plain_text_still_renders(monkeypatch, fake_cli_factory):
    output = _run_single_query(monkeypatch, fake_cli_factory, "plain query")

    assert "Query: plain query" in output
