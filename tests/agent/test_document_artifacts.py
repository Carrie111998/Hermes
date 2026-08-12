"""Profile-scoped artifact persistence for standalone Hermes surfaces."""

from pathlib import Path

import pytest

from agent.document_artifacts import ProfileDocumentArtifactStore


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    import agent.document_artifacts as module

    module.reset_store_cache()
    yield
    module.reset_store_cache()


def test_profile_store_keeps_original_and_processed_in_db_and_locally(tmp_path):
    source = tmp_path / "report.csv"
    source.write_text("name\nWidget\n")
    store = ProfileDocumentArtifactStore()
    record = store.ingest(source, session_id="sid", origin="desktop")
    settled = store.wait_until_settled(record.id, timeout=10)
    assert settled.status == "ready"
    assert store._one("SELECT content FROM artifacts WHERE role='original'")["content"] == source.read_bytes()
    assert Path(settled.processed_path).read_text().find("Widget") >= 0
    store.close()


def test_missing_local_mirror_is_rebuilt_from_sqlite(tmp_path):
    source = tmp_path / "report.csv"
    source.write_text("name\nWidget\n")
    store = ProfileDocumentArtifactStore()
    settled = store.wait_until_settled(
        store.ingest(source, session_id="sid", origin="desktop").id, timeout=10
    )
    processed = Path(settled.processed_path)
    processed.unlink()

    rebuilt = store.processed_path_for(source)
    assert rebuilt is not None and rebuilt.read_text().find("Widget") >= 0
    store.close()


def test_identical_bytes_reuse_the_existing_sidecar(tmp_path):
    first = tmp_path / "a.csv"
    second = tmp_path / "b.csv"
    first.write_text("name\nWidget\n")
    second.write_text("name\nWidget\n")
    store = ProfileDocumentArtifactStore()
    a = store.wait_until_settled(store.ingest(first, session_id="s", origin="cli").id, timeout=10)
    b = store.wait_until_settled(store.ingest(second, session_id="s", origin="cli").id, timeout=10)
    assert a.processed_path == b.processed_path
    store.close()


def test_processed_path_for_returns_none_for_unknown_files(tmp_path):
    unknown = tmp_path / "never-seen.pdf"
    unknown.write_bytes(b"%PDF-1.4")
    store = ProfileDocumentArtifactStore()
    assert store.processed_path_for(unknown) is None
    store.close()


def test_unsupported_binaries_are_not_ingested(tmp_path):
    image = tmp_path / "photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    store = ProfileDocumentArtifactStore()
    assert store.ingest(image, session_id="s", origin="cli") is None
    assert store.processed_path_for(image) is None
    store.close()


def test_failed_processing_settles_without_a_processed_path(tmp_path, monkeypatch):
    source = tmp_path / "locked.pdf"
    source.write_bytes(b"%PDF-1.4 broken")
    store = ProfileDocumentArtifactStore()
    settled = store.wait_until_settled(
        store.ingest(source, session_id="s", origin="cli").id, timeout=10
    )
    assert settled.status in {"needs_attention", "failed"}
    assert settled.processed_path is None
    store.close()


def test_store_is_a_singleton_per_hermes_home(tmp_path, monkeypatch):
    import agent.document_artifacts as module

    first = module.get_store()
    assert module.get_store() is first

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "other"))
    other = module.get_store()
    assert other is not first
    assert other.db_path != first.db_path


def test_database_lives_under_hermes_home_not_dot_hermes(tmp_path):
    store = ProfileDocumentArtifactStore()
    assert str(store.db_path).startswith(str(tmp_path / "home"))
    assert ".hermes" not in str(store.db_path).replace(str(tmp_path), "")
    store.close()
