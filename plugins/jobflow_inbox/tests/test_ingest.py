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
