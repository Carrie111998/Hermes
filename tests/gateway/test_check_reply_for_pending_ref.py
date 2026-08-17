"""Deterministic ref-tag routing for quote-replies to pending cron decisions.

Covers ``gateway.run.check_reply_for_pending_ref()``, the deterministic
helper that replaces pure skill-judgment recognition of a
``[ref:<subsystem>:<id>]`` tag in a quote-reply's quoted text. Background:
the previous behaviour prepended only a soft "[Replying to: ...]" hint and
relied entirely on whichever model was serving the Telegram session to
notice the tag and apply the cron-approval-reply skill on its own — a real
incident showed a session on a weaker/fallback model missing this and
answering as ordinary chat. This helper makes tag detection and
pending-record lookup deterministic code, not model judgment.
"""

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def _stage(subsystem, pending_id, hermes_home):
    """Write a minimal pending record directly, bypassing stage_write()'s
    random id generation so the test controls the id."""
    import json
    d = hermes_home / "pending" / subsystem
    d.mkdir(parents=True, exist_ok=True)
    record = {
        "id": pending_id,
        "subsystem": subsystem,
        "action": "",
        "summary": "test record",
        "origin": "background_review",
        "created_at": 0,
        "payload": {},
    }
    (d / f"{pending_id}.json").write_text(json.dumps(record), encoding="utf-8")


def test_no_tag_returns_not_found(hermes_home):
    from gateway.run import check_reply_for_pending_ref

    result = check_reply_for_pending_ref("just an ordinary quoted message")
    assert result.tag_found is False
    assert result.pending_exists is False
    assert result.subsystem is None
    assert result.pending_id is None


def test_no_reply_text_returns_not_found(hermes_home):
    from gateway.run import check_reply_for_pending_ref

    assert check_reply_for_pending_ref(None).tag_found is False
    assert check_reply_for_pending_ref("").tag_found is False


def test_tag_with_pending_record_present(hermes_home):
    from gateway.run import check_reply_for_pending_ref

    _stage("upstream_fix", "28e9858f", hermes_home)

    text = "\U0001f319 Overnight update — 2026-08-12 [ref:upstream_fix:28e9858f]"
    result = check_reply_for_pending_ref(text)

    assert result.tag_found is True
    assert result.pending_exists is True
    assert result.subsystem == "upstream_fix"
    assert result.pending_id == "28e9858f"


def test_tag_without_pending_record(hermes_home):
    from gateway.run import check_reply_for_pending_ref

    # No record staged for this id — already resolved/discarded/expired.
    text = "\U0001f319 Overnight update [ref:upstream_fix:deadbeef]"
    result = check_reply_for_pending_ref(text)

    assert result.tag_found is True
    assert result.pending_exists is False
    assert result.subsystem == "upstream_fix"
    assert result.pending_id == "deadbeef"


def test_regex_extracts_subsystem_and_id_from_realistic_tag(hermes_home):
    from gateway.run import _REF_TAG_RE

    match = _REF_TAG_RE.search(
        "\U0001f319 Overnight update — 2026-08-12 [ref:upstream_fix:28e9858f]\n\n"
        "✅ Auto-merged: nothing"
    )
    assert match is not None
    assert match.group(1) == "upstream_fix"
    assert match.group(2) == "28e9858f"


def test_regex_does_not_match_malformed_tags(hermes_home):
    from gateway.run import _REF_TAG_RE

    assert _REF_TAG_RE.search("no tag here at all") is None
    assert _REF_TAG_RE.search("[ref:missing-id]") is None
    assert _REF_TAG_RE.search("[ref:]") is None
    assert _REF_TAG_RE.search("ref:upstream_fix:28e9858f") is None  # no brackets


def test_get_pending_lookup_failure_treated_as_no_record(hermes_home, monkeypatch):
    """If the pending-store lookup itself raises, the helper must not crash
    the message-handling path — treat it as "no record" rather than
    propagating, since a broken pending store is not a reason to break
    ordinary message delivery."""
    from gateway.run import check_reply_for_pending_ref
    from tools import write_approval as wa

    def _boom(subsystem, pending_id):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(wa, "get_pending", _boom)

    result = check_reply_for_pending_ref("[ref:upstream_fix:28e9858f]")
    assert result.tag_found is True
    assert result.pending_exists is False
