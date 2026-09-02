"""Tests for cron payload_type plumbing (TKT-0033)."""

import cron.scheduler as scheduler


def test_extract_payload_type_default_returns_text_markdown():
    job = {}
    assert scheduler._extract_payload_type(job) == "text/markdown"


def test_extract_payload_type_explicit_text_html():
    job = {"payload_type": "text/html"}
    assert scheduler._extract_payload_type(job) == "text/html"


def test_extract_payload_type_invalid_coerces_to_text_markdown():
    job = {"payload_type": "xml"}
    assert scheduler._extract_payload_type(job) == "text/markdown"


def test_extract_payload_type_invalid_logs_once_per_job(caplog):
    """Invalid payload_type warns exactly once per job ID per process (#90844 review)."""
    import logging

    scheduler._payload_type_warned.discard("job-warn-test")
    job = {"id": "job-warn-test", "payload_type": "text/md"}
    with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
        assert scheduler._extract_payload_type(job) == "text/markdown"
        assert scheduler._extract_payload_type(job) == "text/markdown"
    warnings = [r for r in caplog.records if "job-warn-test" in r.getMessage()]
    assert len(warnings) == 1
    assert "text/md" in warnings[0].getMessage()


def test_rotate_deadletter_under_cap_noop(tmp_path):
    p = tmp_path / "deadletter.jsonl"
    p.write_text('{"a": 1}\n{"a": 2}\n')
    scheduler._rotate_deadletter(p)
    assert p.read_text() == '{"a": 1}\n{"a": 2}\n'


def test_rotate_deadletter_over_cap_drops_oldest(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "_DEADLETTER_MAX_BYTES", 100)
    p = tmp_path / "deadletter.jsonl"
    lines = [f'{{"n": {i}, "pad": "xxxxxxxxxx"}}\n' for i in range(20)]
    p.write_text("".join(lines))
    assert p.stat().st_size > 100
    scheduler._rotate_deadletter(p)
    kept = p.read_text().splitlines()
    # ~newest half retained, whole lines only, newest record survives
    assert 5 <= len(kept) <= 15
    assert '"n": 19' in kept[-1]
    assert all(l.startswith("{") for l in kept)
