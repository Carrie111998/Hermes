"""Behavior contract for ``utils.prune_oldest_files``.

Shared retention primitive for append-only artifact directories (issue #77472).
Every test drives the real filesystem — no mocks — because the whole point of
the helper is which files actually survive on disk.
"""

import os
import time

import pytest

from utils import prune_oldest_files


def _seed(directory, names, *, start_mtime=1_700_000_000.0, step=10.0):
    """Create *names* in order, oldest first, with explicit spaced mtimes.

    Explicit mtimes rather than real sleeps: filesystem timestamp granularity
    varies (HFS+ 1s, ext3 1s, NTFS 100ns), so writing files back-to-back can
    produce identical mtimes and make ordering assertions flaky.
    """
    directory.mkdir(parents=True, exist_ok=True)
    created = []
    for index, name in enumerate(names):
        path = directory / name
        path.write_text(f"payload {name}", encoding="utf-8")
        stamp = start_mtime + index * step
        os.utime(path, (stamp, stamp))
        created.append(path)
    return created


class TestKeepsNewest:
    def test_keeps_exactly_keep_newest_and_deletes_rest(self, tmp_path):
        names = [f"dump_{i:02d}.json" for i in range(10)]
        _seed(tmp_path, names)

        deleted = prune_oldest_files(tmp_path, "dump_*.json", 3)

        assert deleted == 7
        survivors = sorted(p.name for p in tmp_path.glob("dump_*.json"))
        assert survivors == names[-3:], "the three newest must be the survivors"

    def test_no_op_when_under_cap(self, tmp_path):
        names = [f"dump_{i}.json" for i in range(3)]
        _seed(tmp_path, names)

        assert prune_oldest_files(tmp_path, "dump_*.json", 5) == 0
        assert sorted(p.name for p in tmp_path.glob("dump_*.json")) == sorted(names)

    def test_no_op_when_exactly_at_cap(self, tmp_path):
        names = [f"dump_{i}.json" for i in range(4)]
        _seed(tmp_path, names)

        assert prune_oldest_files(tmp_path, "dump_*.json", 4) == 0
        assert len(list(tmp_path.glob("dump_*.json"))) == 4

    def test_repeated_writes_stay_bounded(self, tmp_path):
        """The invariant that matters: N+k writes leave exactly N on disk."""
        keep = 5
        newest = []
        for i in range(40):
            path = tmp_path / f"dump_{i:03d}.json"
            path.write_text("x", encoding="utf-8")
            stamp = 1_700_000_000.0 + i * 10
            os.utime(path, (stamp, stamp))
            newest.append(path.name)
            prune_oldest_files(tmp_path, "dump_*.json", keep)
            assert len(list(tmp_path.glob("dump_*.json"))) <= keep

        assert sorted(p.name for p in tmp_path.glob("dump_*.json")) == sorted(newest[-keep:])


class TestOrderingIsByMtimeNotName:
    def test_variable_prefix_does_not_corrupt_ordering(self, tmp_path):
        """Reverse-lexical sorting is wrong when a variable field precedes the timestamp.

        Request-dump filenames are ``request_dump_<session>_<timestamp>.json``.
        Sorting those by name orders by session id first, so the newest dump of
        an alphabetically-early session would be deleted while an older dump of
        a later session survived. mtime ordering is the contract.
        """
        # 'zzz' session is OLDEST, 'aaa' session is NEWEST — name order is the
        # exact inverse of age.
        _seed(
            tmp_path,
            [
                "request_dump_zzz_20260101_000000_000000.json",
                "request_dump_mmm_20260101_000000_000000.json",
                "request_dump_aaa_20260101_000000_000000.json",
            ],
        )

        deleted = prune_oldest_files(tmp_path, "request_dump_*.json", 1)

        assert deleted == 2
        survivors = [p.name for p in tmp_path.glob("request_dump_*.json")]
        assert survivors == ["request_dump_aaa_20260101_000000_000000.json"]

    def test_tie_on_mtime_is_deterministic(self, tmp_path):
        """Identical mtimes (coarse filesystems) must not produce arbitrary results."""
        paths = _seed(tmp_path, [f"dump_{i}.json" for i in range(6)], step=0.0)
        for path in paths:
            os.utime(path, (1_700_000_000.0, 1_700_000_000.0))

        first = prune_oldest_files(tmp_path, "dump_*.json", 2)
        survivors_a = sorted(p.name for p in tmp_path.glob("dump_*.json"))

        # Same seed, same expectation — the name tiebreaker makes it repeatable.
        for path in tmp_path.glob("dump_*.json"):
            path.unlink()
        paths = _seed(tmp_path, [f"dump_{i}.json" for i in range(6)], step=0.0)
        for path in paths:
            os.utime(path, (1_700_000_000.0, 1_700_000_000.0))
        second = prune_oldest_files(tmp_path, "dump_*.json", 2)
        survivors_b = sorted(p.name for p in tmp_path.glob("dump_*.json"))

        assert first == second == 4
        assert survivors_a == survivors_b


class TestProtectedFileSurvives:
    """``protect`` exempts the file the caller just wrote.

    Newest-first ordering usually spares it, but when mtimes tie the whole
    filename breaks the tie — and for ``request_dump_<session>_<ts>.json`` the
    session id leads, so a sibling session's id can sort higher and evict the
    fresh dump. These assert the invariant the caller depends on ("the file I
    just wrote is still there when this returns"), not a sort order.
    """

    def test_protected_file_survives_an_mtime_tie_it_would_lose(self, tmp_path):
        # Same mtime for all four; the protected name sorts LAST lexically, so
        # without the exemption the tiebreaker deletes it.
        losers = [f"request_dump_zzz_{i:02d}.json" for i in range(3)]
        just_written = "request_dump_aaa_99.json"
        for path in _seed(tmp_path, [*losers, just_written], step=0.0):
            os.utime(path, (1_700_000_000.0, 1_700_000_000.0))
        target = tmp_path / just_written

        prune_oldest_files(tmp_path, "request_dump_*.json", 1, protect=target)

        assert target.exists(), "the file the caller just wrote must survive"
        assert [p.name for p in tmp_path.glob("request_dump_*.json")] == [just_written]

    def test_protected_file_still_counts_against_the_cap(self, tmp_path):
        """The exemption must not let the directory exceed *keep*."""
        names = [f"dump_{i}.json" for i in range(10)]
        _seed(tmp_path, names)
        target = tmp_path / names[0]  # oldest, so it would normally be deleted

        prune_oldest_files(tmp_path, "dump_*.json", 3, protect=target)

        survivors = sorted(p.name for p in tmp_path.glob("dump_*.json"))
        assert len(survivors) == 3, "cap still holds exactly"
        assert names[0] in survivors, "protected file is one of the survivors"

    def test_protected_name_in_another_directory_is_not_credited(self, tmp_path):
        """A same-named file elsewhere must not exempt a local file by name."""
        other = tmp_path / "other"
        other.mkdir()
        _seed(tmp_path, [f"dump_{i}.json" for i in range(5)])

        deleted = prune_oldest_files(
            tmp_path, "dump_*.json", 2, protect=other / "dump_0.json",
        )

        assert deleted == 3
        assert len(list(tmp_path.glob("dump_*.json"))) == 2

    def test_protect_of_a_missing_file_keeps_the_full_cap(self, tmp_path):
        """Nothing to exempt → no slot is reserved, so *keep* files remain."""
        _seed(tmp_path, [f"dump_{i}.json" for i in range(6)])

        prune_oldest_files(
            tmp_path, "dump_*.json", 3, protect=tmp_path / "dump_never_written.json",
        )

        assert len(list(tmp_path.glob("dump_*.json"))) == 3

    def test_protect_none_is_the_previous_behavior(self, tmp_path):
        _seed(tmp_path, [f"dump_{i}.json" for i in range(6)])

        assert prune_oldest_files(tmp_path, "dump_*.json", 2, protect=None) == 4
        assert len(list(tmp_path.glob("dump_*.json"))) == 2

    def test_under_cap_with_protect_deletes_nothing(self, tmp_path):
        paths = _seed(tmp_path, [f"dump_{i}.json" for i in range(3)])

        assert prune_oldest_files(tmp_path, "dump_*.json", 5, protect=paths[-1]) == 0
        assert len(list(tmp_path.glob("dump_*.json"))) == 3

    def test_keep_one_with_protect_leaves_only_the_protected_file(self, tmp_path):
        paths = _seed(tmp_path, [f"dump_{i}.json" for i in range(4)])
        target = paths[1]

        prune_oldest_files(tmp_path, "dump_*.json", 1, protect=target)

        assert [p.name for p in tmp_path.glob("dump_*.json")] == [target.name]

    def test_garbage_protect_value_does_not_break_pruning(self, tmp_path):
        _seed(tmp_path, [f"dump_{i}.json" for i in range(5)])

        assert prune_oldest_files(tmp_path, "dump_*.json", 2, protect=object()) == 3


class TestScopeSafety:
    def test_only_matching_pattern_is_touched(self, tmp_path):
        _seed(tmp_path, [f"dump_{i}.json" for i in range(5)])
        keepers = ["state.db", "session_abc.json", "notes.txt", "dump_5.json.bak"]
        for name in keepers:
            (tmp_path / name).write_text("keep me", encoding="utf-8")

        prune_oldest_files(tmp_path, "dump_*.json", 1)

        for name in keepers:
            assert (tmp_path / name).exists(), f"{name} must not be pruned"

    def test_does_not_recurse_into_subdirectories(self, tmp_path):
        nested = tmp_path / "nested"
        _seed(nested, [f"dump_{i}.json" for i in range(5)])
        _seed(tmp_path, [f"dump_{i}.json" for i in range(5)])

        prune_oldest_files(tmp_path, "dump_*.json", 1)

        assert len(list(nested.glob("dump_*.json"))) == 5, "subdirectory untouched"
        assert len(list(tmp_path.glob("dump_*.json"))) == 1

    def test_directories_matching_pattern_are_skipped(self, tmp_path):
        (tmp_path / "dump_dir.json").mkdir(parents=True)
        _seed(tmp_path, [f"dump_{i}.json" for i in range(4)])

        prune_oldest_files(tmp_path, "dump_*.json", 1)

        assert (tmp_path / "dump_dir.json").is_dir(), "directory must survive"

    @pytest.mark.skipif(
        not hasattr(os, "symlink"), reason="platform has no symlink support"
    )
    def test_symlinks_are_never_followed_or_deleted(self, tmp_path):
        """A symlink in the pruned directory must not let deletion escape it."""
        outside = tmp_path / "outside"
        outside.mkdir()
        precious = outside / "precious.json"
        precious.write_text("do not delete", encoding="utf-8")

        pruned = tmp_path / "pruned"
        _seed(pruned, [f"dump_{i}.json" for i in range(4)])
        link = pruned / "dump_link.json"
        try:
            link.symlink_to(precious)
        except (OSError, NotImplementedError):  # pragma: no cover - Windows w/o privilege
            pytest.skip("symlink creation not permitted on this platform")

        prune_oldest_files(pruned, "dump_*.json", 1)

        assert precious.exists(), "symlink target outside the directory must survive"
        assert precious.read_text(encoding="utf-8") == "do not delete"
        assert link.is_symlink(), "the symlink itself must be left alone"


class TestDisableAndFailureModes:
    @pytest.mark.parametrize("keep", [0, -1, -100])
    def test_non_positive_keep_disables_pruning(self, tmp_path, keep):
        names = [f"dump_{i}.json" for i in range(6)]
        _seed(tmp_path, names)

        assert prune_oldest_files(tmp_path, "dump_*.json", keep) == 0
        assert len(list(tmp_path.glob("dump_*.json"))) == 6

    def test_missing_directory_is_not_an_error(self, tmp_path):
        assert prune_oldest_files(tmp_path / "nope", "dump_*.json", 3) == 0

    def test_empty_directory_is_not_an_error(self, tmp_path):
        assert prune_oldest_files(tmp_path, "dump_*.json", 3) == 0

    def test_unlink_failure_does_not_abort_the_sweep(self, tmp_path, monkeypatch):
        """One undeletable file (Windows open handle) must not strand the rest."""
        names = [f"dump_{i:02d}.json" for i in range(6)]
        _seed(tmp_path, names)

        real_unlink = os.unlink
        blocked = tmp_path / names[0]

        def flaky_unlink(target, *args, **kwargs):
            if str(target) == str(blocked):
                raise PermissionError("file is open in another process")
            return real_unlink(target, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", flaky_unlink)

        deleted = prune_oldest_files(tmp_path, "dump_*.json", 2)

        # 6 files, keep 2 -> 4 targeted, 1 refuses -> 3 deleted, no exception.
        assert deleted == 3
        assert blocked.exists(), "undeletable file remains, deferred to next prune"
        assert sorted(p.name for p in tmp_path.glob("dump_*.json")) == sorted(
            [names[0], names[-2], names[-1]]
        )

    def test_file_vanishing_mid_scan_is_tolerated(self, tmp_path):
        """Concurrent prune from another process removes a candidate underneath us."""
        names = [f"dump_{i}.json" for i in range(5)]
        paths = _seed(tmp_path, names)
        paths[0].unlink()  # gone before the sweep even starts

        deleted = prune_oldest_files(tmp_path, "dump_*.json", 1)

        assert deleted == 3
        assert len(list(tmp_path.glob("dump_*.json"))) == 1

    def test_returns_int_and_never_raises_on_bad_input(self, tmp_path):
        # A file where a directory is expected: glob raises NotADirectoryError on
        # some platforms and yields nothing on others; either way, no traceback.
        target = tmp_path / "a_file"
        target.write_text("x", encoding="utf-8")
        assert prune_oldest_files(target, "dump_*.json", 2) == 0


def test_real_clock_ordering_matches_write_order(tmp_path):
    """Sanity check against the real clock, not just synthetic mtimes."""
    written = []
    for i in range(4):
        path = tmp_path / f"dump_{i}.json"
        path.write_text("x", encoding="utf-8")
        written.append(path)
        time.sleep(0.02)

    prune_oldest_files(tmp_path, "dump_*.json", 2)
    survivors = {p.name for p in tmp_path.glob("dump_*.json")}

    assert survivors == {written[-2].name, written[-1].name}
