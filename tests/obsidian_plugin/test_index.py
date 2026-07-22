from plugins.memory.obsidian.index import ObsidianIndex


def _idx():
    return ObsidianIndex(":memory:")


def test_upsert_creates_chunk_rows():
    idx = _idx()
    idx.upsert_note("memory/daniel.md", "# Daniel\ngillar kaffe\n", 100.0)
    paths = idx.indexed_paths()
    assert "memory/daniel.md" in paths
    mtime, chash = paths["memory/daniel.md"]
    assert mtime == 100.0
    assert len(chash) == 64  # sha256 hex


def test_upsert_replaces_previous_rows_for_path():
    idx = _idx()
    idx.upsert_note("a.md", "# A\nförsta\n", 1.0)
    idx.upsert_note("a.md", "# A\nandra\n", 2.0)
    # only the new content remains; mtime updated
    assert idx.indexed_paths()["a.md"][0] == 2.0
    # search proves old content is gone (Task 5 adds search; here check row count)
    assert idx._chunk_count_for("a.md") == 1


def test_delete_note_removes_rows():
    idx = _idx()
    idx.upsert_note("a.md", "# A\nx\n", 1.0)
    idx.delete_note("a.md")
    assert "a.md" not in idx.indexed_paths()
    assert idx._chunk_count_for("a.md") == 0


def test_content_hash_changes_with_content():
    idx = _idx()
    idx.upsert_note("a.md", "# A\nett\n", 1.0)
    h1 = idx.indexed_paths()["a.md"][1]
    idx.upsert_note("a.md", "# A\ntvå\n", 1.0)
    h2 = idx.indexed_paths()["a.md"][1]
    assert h1 != h2
