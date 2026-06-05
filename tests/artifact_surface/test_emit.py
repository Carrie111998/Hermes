import json
import pytest
import hermes_artifacts
from artifact_surface import store


@pytest.fixture
def artdir(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes_artifacts, "artifacts_dir", lambda: tmp_path)
    monkeypatch.setattr(store, "artifacts_dir", lambda: tmp_path)
    return tmp_path


def test_emit_writes_html_and_manifest(artdir):
    slug = hermes_artifacts.emit("<h1>hi</h1>", title="My Report", source="scout",
                                 tags=["ops"], summary="s", refresh_secs=10,
                                 data_endpoints=["/api/cron"])
    assert slug == "my-report"
    assert (artdir / "my-report.html").read_text(encoding="utf-8") == "<h1>hi</h1>"
    manifest = json.loads((artdir / "my-report.json").read_text(encoding="utf-8"))
    assert manifest["title"] == "My Report"
    assert manifest["source"] == "scout"
    assert manifest["data_endpoints"] == ["/api/cron"]
    assert manifest["created_at"]


def test_emit_explicit_id_overrides_slug(artdir):
    slug = hermes_artifacts.emit("<p>x</p>", title="Anything", source="cron", id="fixed-id")
    assert slug == "fixed-id"
    assert (artdir / "fixed-id.html").exists()


def test_emitted_artifact_is_listed_by_store(artdir):
    hermes_artifacts.emit("<p>x</p>", title="Listed", source="scout")
    ids = [m.id for m in store.list_artifacts()]
    assert "listed" in ids


def test_emit_rejects_unsafe_explicit_id(artdir):
    import pytest as _pytest
    with _pytest.raises(ValueError):
        hermes_artifacts.emit("<p>x</p>", title="X", source="s", id="../evil")
    with _pytest.raises(ValueError):
        hermes_artifacts.emit("<p>x</p>", title="X", source="s", id="a/b")


def test_emit_safe_explicit_id_ok(artdir):
    slug = hermes_artifacts.emit("<p>x</p>", title="X", source="s", id="ok.id-1")
    assert slug == "ok.id-1"
