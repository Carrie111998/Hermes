import json
import pytest
from artifact_surface import store


@pytest.fixture
def artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "artifacts_dir", lambda: tmp_path)
    return tmp_path


def _write(d, slug, html, manifest=None):
    (d / f"{slug}.html").write_text(html, encoding="utf-8")
    if manifest is not None:
        (d / f"{slug}.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_list_artifacts_newest_first(artifacts):
    _write(artifacts, "a", "<p>a</p>", {"id": "a", "title": "A", "created_at": "2026-06-01T00:00:00+00:00"})
    _write(artifacts, "b", "<p>b</p>", {"id": "b", "title": "B", "created_at": "2026-06-05T00:00:00+00:00"})
    items = store.list_artifacts()
    assert [m.id for m in items] == ["b", "a"]


def test_get_artifact_html(artifacts):
    _write(artifacts, "a", "<p>hello</p>", {"id": "a", "title": "A"})
    assert store.get_artifact_html("a") == "<p>hello</p>"


def test_get_artifact_html_missing_returns_none(artifacts):
    assert store.get_artifact_html("nope") is None


def test_id_traversal_rejected(artifacts):
    assert store.get_artifact_html("../secrets") is None
    assert store.get_artifact_html("a/b") is None
