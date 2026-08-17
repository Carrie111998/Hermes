import json
from artifact_surface.manifest import parse_manifest


def test_parse_full_manifest(tmp_path):
    p = tmp_path / "ops.json"
    p.write_text(json.dumps({
        "id": "ops", "title": "Ops Overview", "source": "scout",
        "created_at": "2026-06-05T12:00:00-04:00", "tags": ["ops"],
        "summary": "live", "refresh_secs": 15, "data_endpoints": ["/api/cron"],
    }), encoding="utf-8")
    m = parse_manifest(p)
    assert m.id == "ops"
    assert m.title == "Ops Overview"
    assert m.tags == ["ops"]
    assert m.refresh_secs == 15


def test_parse_missing_sidecar_derives_from_html(tmp_path):
    html = tmp_path / "my-report.html"
    html.write_text("<h1>hi</h1>", encoding="utf-8")
    m = parse_manifest(html.with_suffix(".json"), html_path=html)
    assert m.id == "my-report"
    assert m.title == "my-report"
    assert m.source == "unknown"
    assert m.created_at  # derived from mtime, ISO string


def test_malformed_sidecar_falls_back(tmp_path):
    html = tmp_path / "x.html"
    html.write_text("<p>x</p>", encoding="utf-8")
    bad = tmp_path / "x.json"
    bad.write_text("{not json", encoding="utf-8")
    m = parse_manifest(bad, html_path=html)
    assert m.id == "x"  # falls back to filename


def test_pinned_defaults_false(tmp_path):
    html = tmp_path / "x.html"
    html.write_text("<p>x</p>", encoding="utf-8")
    m = parse_manifest(html.with_suffix(".json"), html_path=html)
    assert m.pinned is False


def test_parse_pinned_true(tmp_path):
    p = tmp_path / "ops.json"
    p.write_text(json.dumps({"id": "ops", "title": "Ops", "pinned": True}), encoding="utf-8")
    m = parse_manifest(p)
    assert m.pinned is True
    assert m.to_dict()["pinned"] is True
