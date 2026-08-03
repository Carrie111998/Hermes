"""Unit tests for gateway.membrane_outbound outbox."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gateway import membrane_outbound as mo


@pytest.fixture()
def tmp_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # hermes_constants reads env at call time via get_hermes_home
    yield home


def test_parse_telegram_chat_id():
    assert mo.parse_telegram_chat_id("telegram:dm:8078895371") == "8078895371"
    assert mo.parse_telegram_chat_id("8078895371") == "8078895371"
    assert mo.parse_telegram_chat_id("api_server:foo") is None
    assert mo.parse_telegram_chat_id("") is None


def test_enqueue_list_claim_ack(tmp_hermes_home):
    rid = mo.enqueue(
        chat_id="telegram:dm:42",
        content="hello cron",
        metadata={"source": "test"},
    )
    assert rid and rid.startswith("mo_")
    pending = mo.list_pending()
    assert len(pending) == 1
    assert pending[0]["chat_id"] == "42"
    assert pending[0]["content"] == "hello cron"

    claimed = mo.claim([rid])
    assert claimed == [rid]

    n = mo.ack([rid], ok=True)
    assert n == 1
    assert mo.list_pending() == []


def test_ack_failure_requeues(tmp_hermes_home):
    rid = mo.enqueue(chat_id="42", content="retry me")
    mo.claim([rid])
    mo.ack([rid], ok=False, error="bot 429")
    pending = mo.list_pending()
    assert len(pending) == 1
    assert pending[0]["id"] == rid
