from pathlib import Path

from plugins.memory.obsidian.index import ObsidianIndex


def _write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_sync_indexes_new_files(tmp_path):
    _write(tmp_path / "memory" / "daniel.md", "# Daniel\nkaffe\n")
    _write(tmp_path / "projekt" / "saa.md", "# SAA\nbundle\n")
    idx = ObsidianIndex(":memory:")
    summary = idx.sync_vault(str(tmp_path))
    assert summary["added"] == 2
    assert set(idx.indexed_paths()) == {"memory/daniel.md", "projekt/saa.md"}


def test_sync_skips_unchanged_and_updates_changed(tmp_path):
    f = tmp_path / "a.md"
    _write(f, "# A\nett\n")
    idx = ObsidianIndex(":memory:")
    idx.sync_vault(str(tmp_path))
    # unchanged run
    s2 = idx.sync_vault(str(tmp_path))
    assert s2["unchanged"] == 1 and s2["updated"] == 0
    # change the file
    _write(f, "# A\ntvå\n")
    s3 = idx.sync_vault(str(tmp_path))
    assert s3["updated"] == 1


def test_sync_deletes_removed_files(tmp_path):
    f = tmp_path / "a.md"
    _write(f, "# A\nx\n")
    idx = ObsidianIndex(":memory:")
    idx.sync_vault(str(tmp_path))
    f.unlink()
    s = idx.sync_vault(str(tmp_path))
    assert s["deleted"] == 1
    assert "a.md" not in idx.indexed_paths()


def test_sync_excludes_git_and_obsidian_dirs(tmp_path):
    _write(tmp_path / ".git" / "x.md", "# git\n")
    _write(tmp_path / ".obsidian" / "y.md", "# cfg\n")
    _write(tmp_path / "real.md", "# real\n")
    idx = ObsidianIndex(":memory:")
    idx.sync_vault(str(tmp_path))
    assert set(idx.indexed_paths()) == {"real.md"}
