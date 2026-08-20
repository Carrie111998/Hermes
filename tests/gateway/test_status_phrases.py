import random

from gateway.status_phrases import (
    classify_status_context,
    choose_status_phrase,
    resolve_status_phrase_catalog,
    status_phrase_catalog_is_custom,
)


def test_long_running_context_uses_status_bucket():
    assert classify_status_context("status") == "status"
    assert classify_status_context("heartbeat") == "status"
    assert classify_status_context("long_running") == "status"


def test_status_phrase_does_not_leak_raw_preview_or_args():
    msg = choose_status_phrase(
        "status",
        preview="actual private scratch text should not be sent",
        args={"secret": "SECRET-123"},
        rng=random.Random(4),
    )

    assert "actual private scratch" not in msg
    assert "SECRET-123" not in msg
    assert msg


def test_status_phrase_path_can_load_relative_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    phrase_dir = tmp_path / "phrase-catalog"
    phrase_dir.mkdir()
    (phrase_dir / "01-status.yaml").write_text("status:\n  - relative dir status text\n", encoding="utf-8")

    catalog = resolve_status_phrase_catalog(
        {"display": {"status_phrases": {"path": "phrase-catalog"}}},
        "whatsapp",
    )

    assert "relative dir status text" in catalog["status"]


def test_choose_status_phrase_uses_custom_catalog_without_leaking_args():
    catalog = resolve_status_phrase_catalog(
        {"display": {"status_phrases": {"mode": "replace", "status": ["custom safe status text"]}}},
        "whatsapp",
    )

    msg = choose_status_phrase(
        "status",
        args={"query": "SECRET SEARCH"},
        catalog=catalog,
    )

    assert msg == "custom safe status text"
    assert "SECRET" not in msg


def test_custom_catalog_detection_distinguishes_builtins_from_user_phrases():
    builtins = resolve_status_phrase_catalog({}, "telegram")
    custom = resolve_status_phrase_catalog(
        {"display": {"status_phrases": {"mode": "replace", "status": ["arbeite noch"]}}},
        "telegram",
    )

    assert status_phrase_catalog_is_custom(builtins) is False
    assert status_phrase_catalog_is_custom(custom) is True
