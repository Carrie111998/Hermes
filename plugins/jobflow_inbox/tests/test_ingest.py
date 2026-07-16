import json

from plugins.jobflow_inbox import ingest, extract


def _write_pipeline(tmp_path, jobs):
    p = tmp_path / "pipeline.json"
    p.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    return p


def test_duplicate_match_on_normalized_url(tmp_path):
    p = _write_pipeline(tmp_path, {
        "j1": {"url": "https://boards.greenhouse.io/acme/jobs/123/?utm_source=x"}
    })
    assert ingest.is_duplicate("https://boards.greenhouse.io/acme/jobs/123", p) is True


def test_not_duplicate(tmp_path):
    p = _write_pipeline(tmp_path, {"j1": {"url": "https://x.test/a"}})
    assert ingest.is_duplicate("https://x.test/b", p) is False


def test_malformed_pipeline_is_not_duplicate(tmp_path):
    p = tmp_path / "pipeline.json"
    p.write_text("{ this is not json", encoding="utf-8")
    assert ingest.is_duplicate("https://x.test/a", p) is False


def test_missing_pipeline_is_not_duplicate(tmp_path):
    assert ingest.is_duplicate("https://x.test/a", tmp_path / "nope.json") is False


def test_build_message_shape():
    jf = extract.JobFields(title="Data Engineer", company="Acme",
                           location="Austin, TX", salary="USD 150000-190000 YEAR",
                           description="do things", enrichment_status="enriched")
    msg = ingest.build_message(
        jf, url="https://x.test/j/1?utm_source=y",
        normalized_url="https://x.test/j/1",
        cid="11111111-2222-3333-4444-555555555555",
        message_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        ts_iso="2026-07-16T12:00:00+00:00",
    )
    assert msg["type"] == "USER_SUBMITTED_JOB"
    assert msg["to"] == "tracker"
    assert msg["from"] == "jobflow_inbox"
    assert msg["protocol_version"] == "2.0"
    job = msg["payload"]["job"]
    assert job["source"] == "user-submitted"
    assert job["user_submitted"] is True
    assert job["fast_track"] is True
    assert job["title"] == "Data Engineer"
    assert job["url"] == "https://x.test/j/1?utm_source=y"
    assert msg["idempotency_key"].startswith("user_submitted:")


def test_idempotency_key_stable_across_calls():
    jf = extract.JobFields(enrichment_status="failed")
    kwargs = dict(url="https://x.test/j/1", normalized_url="https://x.test/j/1",
                  cid="c", message_id="m", ts_iso="2026-07-16T12:00:00+00:00")
    a = ingest.build_message(jf, **kwargs)["idempotency_key"]
    b = ingest.build_message(jf, **kwargs)["idempotency_key"]
    assert a == b


def test_write_to_tracker_inbox_atomic(tmp_path):
    msg = {"type": "USER_SUBMITTED_JOB", "correlation_id": "abcd1234-x",
           "timestamp": "2026-07-16T12:00:00+00:00"}
    inbox = tmp_path / "inbox"
    fname = ingest.write_to_tracker_inbox(msg, inbox)
    assert "_INTENT_" not in fname.upper()
    assert "USER_SUBMITTED_JOB" in fname
    written = json.loads((inbox / fname).read_text(encoding="utf-8"))
    assert written["type"] == "USER_SUBMITTED_JOB"
    assert not list(inbox.glob("*.tmp"))  # no leftover temp files


import datetime


class _Ids:
    def __call__(self):
        return ("cid00000-1111", "mid00000-2222")


def _now():
    return datetime.datetime(2026, 7, 16, 12, 0, 0, tzinfo=datetime.timezone.utc)


def test_ingest_invalid_url(tmp_path):
    r = ingest.ingest_job("no link", pipeline_path=tmp_path / "p.json",
                          inbox_dir=tmp_path / "inbox")
    assert r.status == "invalid"
    assert "/job <url>" in r.reply


def test_ingest_duplicate(tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"jobs": {"j": {"url": "https://x.test/a"}}}), encoding="utf-8")
    r = ingest.ingest_job("https://x.test/a", pipeline_path=p, inbox_dir=tmp_path / "inbox")
    assert r.status == "duplicate"
    assert "Already tracking" in r.reply


def test_ingest_added_enriched(tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"jobs": {}}), encoding="utf-8")
    inbox = tmp_path / "inbox"
    jf = extract.JobFields(title="Data Engineer", company="Acme",
                           enrichment_status="enriched")
    r = ingest.ingest_job("https://x.test/j/1", pipeline_path=p, inbox_dir=inbox,
                          extract_fn=lambda u: jf, now=_now, ids=_Ids())
    assert r.status == "added"
    assert "Data Engineer" in r.reply and "Acme" in r.reply
    assert len(list(inbox.glob("*_USER_SUBMITTED_JOB_*.json"))) == 1


def test_ingest_url_only_on_failed_extract(tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"jobs": {}}), encoding="utf-8")
    inbox = tmp_path / "inbox"
    jf = extract.JobFields(enrichment_status="failed")
    r = ingest.ingest_job("https://x.test/j/2", pipeline_path=p, inbox_dir=inbox,
                          extract_fn=lambda u: jf, now=_now, ids=_Ids())
    assert r.status == "url_only"
    assert "queued the URL only" in r.reply
    assert len(list(inbox.glob("*_USER_SUBMITTED_JOB_*.json"))) == 1
