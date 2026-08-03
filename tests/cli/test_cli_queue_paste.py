"""Regression tests for private collapsed-paste references in CLI admission."""

from unittest.mock import patch

import hermes_constants

from cli import HermesCLI


def _store_reference(cli_obj: HermesCLI, text: str) -> str:
    reference = cli_obj._store_private_paste_reference(
        text,
        display_index=1,
        line_count=text.count("\n") + 1,
    )
    assert reference is not None
    return reference


def test_private_collapsed_paste_reference_expands_and_is_consumed(
    tmp_path,
    monkeypatch,
):
    pasted = "first\nmiddle\nlast"
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    cli_obj = HermesCLI.__new__(HermesCLI)
    reference = _store_reference(cli_obj, pasted)

    assert cli_obj._expand_paste_references(reference) == pasted
    assert list((tmp_path / "pastes").glob("*.txt")) == []


def test_queue_rejects_private_paste_without_consuming_it(tmp_path, monkeypatch):
    pasted = "first\nmiddle\nlast"
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    cli_obj = HermesCLI.__new__(HermesCLI)
    reference = _store_reference(cli_obj, pasted)

    with patch("cli._cprint"):
        assert cli_obj._handle_explicit_durable_command_inline(
            f"/queue {reference}"
        ) is False

    artifacts = list((tmp_path / "pastes").glob("*.txt"))
    assert len(artifacts) == 1
    assert artifacts[0].read_text(encoding="utf-8") == pasted
