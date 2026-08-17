import json
from datetime import datetime, timedelta, timezone


from artifact_surface import retention


NOW = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _write(d, slug, *, age_days=0, source="scout", pinned=False, sidecar=True):
    (d / f"{slug}.html").write_text(f"<p>{slug}</p>", encoding="utf-8")
    if sidecar:
        manifest = {
            "id": slug, "title": slug, "source": source,
            "created_at": _iso(NOW - timedelta(days=age_days)),
        }
        if pinned:
            manifest["pinned"] = True
        (d / f"{slug}.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_no_policy_keeps_everything(tmp_path):
    _write(tmp_path, "a", age_days=999)
    _write(tmp_path, "b", age_days=0)
    result = retention.prune(tmp_path, now=NOW)
    assert result.deleted == []
    assert (tmp_path / "a.html").exists()
    assert (tmp_path / "b.html").exists()


def test_age_based_deletes_old(tmp_path):
    _write(tmp_path, "old", age_days=40)
    _write(tmp_path, "fresh", age_days=5)
    result = retention.prune(tmp_path, max_age_days=30, now=NOW)
    assert result.deleted == ["old"]
    assert not (tmp_path / "old.html").exists()
    assert (tmp_path / "fresh.html").exists()


def test_age_based_removes_both_html_and_sidecar(tmp_path):
    _write(tmp_path, "old", age_days=40)
    retention.prune(tmp_path, max_age_days=30, now=NOW)
    assert not (tmp_path / "old.html").exists()
    assert not (tmp_path / "old.json").exists()


def test_count_based_keeps_newest_n_per_source(tmp_path):
    # source A: 3 artifacts, keep newest 2
    _write(tmp_path, "a1", age_days=3, source="A")
    _write(tmp_path, "a2", age_days=2, source="A")
    _write(tmp_path, "a3", age_days=1, source="A")
    # source B: 1 artifact, untouched
    _write(tmp_path, "b1", age_days=10, source="B")
    result = retention.prune(tmp_path, max_per_source=2, now=NOW)
    assert result.deleted == ["a1"]  # oldest in source A
    assert (tmp_path / "a2.html").exists()
    assert (tmp_path / "a3.html").exists()
    assert (tmp_path / "b1.html").exists()


def test_pinned_survives_age_policy(tmp_path):
    _write(tmp_path, "pinned-old", age_days=999, pinned=True)
    _write(tmp_path, "old", age_days=999)
    result = retention.prune(tmp_path, max_age_days=30, now=NOW)
    assert result.deleted == ["old"]
    assert (tmp_path / "pinned-old.html").exists()
    assert result.pinned_kept == 1


def test_pinned_survives_count_policy(tmp_path):
    _write(tmp_path, "p1", age_days=5, source="A", pinned=True)
    _write(tmp_path, "a2", age_days=2, source="A")
    _write(tmp_path, "a3", age_days=1, source="A")
    # newest-1 would normally drop p1 and a2; pinned p1 must survive
    result = retention.prune(tmp_path, max_per_source=1, now=NOW)
    assert "p1" not in result.deleted
    assert (tmp_path / "p1.html").exists()


def test_dry_run_reports_but_does_not_delete(tmp_path):
    _write(tmp_path, "old", age_days=40)
    result = retention.prune(tmp_path, max_age_days=30, now=NOW, dry_run=True)
    assert result.deleted == ["old"]
    assert result.dry_run is True
    assert (tmp_path / "old.html").exists()  # still there


def test_bare_html_uses_mtime(tmp_path):
    # No sidecar; created_at derived from file mtime (recent) so it survives age policy.
    _write(tmp_path, "bare", sidecar=False)
    result = retention.prune(tmp_path, max_age_days=30, now=NOW)
    assert result.deleted == []
    assert (tmp_path / "bare.html").exists()


def test_missing_directory_is_noop(tmp_path):
    result = retention.prune(tmp_path / "does-not-exist", max_age_days=30, now=NOW)
    assert result.deleted == []
    assert result.kept == 0


def test_gitkeep_and_non_html_untouched(tmp_path):
    (tmp_path / ".gitkeep").write_text("", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("hi", encoding="utf-8")
    _write(tmp_path, "old", age_days=40)
    retention.prune(tmp_path, max_age_days=30, now=NOW)
    assert (tmp_path / ".gitkeep").exists()
    assert (tmp_path / "notes.txt").exists()


def test_combined_policies_union_of_deletes(tmp_path):
    # too old (age) -> deleted; beyond count -> deleted; recent & within count -> kept
    _write(tmp_path, "stale", age_days=40, source="A")
    _write(tmp_path, "a2", age_days=3, source="A")
    _write(tmp_path, "a3", age_days=2, source="A")
    _write(tmp_path, "a4", age_days=1, source="A")
    result = retention.prune(tmp_path, max_age_days=30, max_per_source=2, now=NOW)
    # source A newest->oldest: a4, a3, a2, stale. max_per_source=2 keeps a4,a3;
    # a2 + stale are count-stale; stale is also age-stale. Union deletes a2 + stale.
    assert "stale" in result.deleted
    assert (tmp_path / "a3.html").exists()
    assert (tmp_path / "a4.html").exists()
    assert not (tmp_path / "a2.html").exists()
