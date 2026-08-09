"""Regression tests for ``agent.file_safety.safe_extract_tar``.

``tarfile``'s ``filter="data"`` landed in 3.11.4 while ``requires-python`` is
``>=3.11``, so 3.11.0–3.11.3 have no filter. This module's contract is that
extraction is safe on those interpreters *too* — not by validating members and
then calling an unfiltered ``extractall``, but by never calling it.

The tests therefore assert the property that matters — **nothing lands outside
the destination** — by extracting for real and looking at the filesystem,
rather than asserting that a particular check fires. Three separate bypasses of
the previous lexical check are pinned here as escape scenarios; each one passed
that check while writing outside.

Its live consumer is ``agent.curator_backup``, which extracts into the
**skills** directory — executable content, not a throwaway temp dir.
"""

import io
import os
import sys
import tarfile
from pathlib import Path

import pytest

from agent import file_safety
from agent.file_safety import safe_extract_tar

# The manual path is only reachable on 3.11.0-3.11.3. Everywhere else the real
# filter runs, so force the manual path too and assert both are safe.
FORCE_MANUAL = [False, True]

# The two Unicode spellings of the same rendered name, written as escapes so no
# editor or formatter can silently normalize one into the other and leave the
# tests below asserting nothing. One filename on macOS, two on Linux.
NFC = "\u00e9"
NFD = "e\u0301"


@pytest.fixture(params=FORCE_MANUAL, ids=["stdlib-filter", "manual-fallback"])
def extract(request, monkeypatch):
    """Run ``safe_extract_tar`` via the stdlib filter and via the fallback."""
    if request.param:
        real = tarfile.TarFile.extractall

        def no_filter(self, path=".", members=None, **kwargs):
            if "filter" in kwargs:
                raise TypeError("extractall() got an unexpected keyword argument 'filter'")
            return real(self, path, members, **kwargs)

        monkeypatch.setattr(tarfile.TarFile, "extractall", no_filter)
    return safe_extract_tar


def _tar_with(path: Path, *members: tarfile.TarInfo) -> Path:
    """Write a tarball whose members are used verbatim.

    Each ``TarInfo`` is built by hand rather than via ``gettarinfo(arcname=…)``,
    because that helper normalizes a leading ``/`` away — which would silently
    turn an absolute-path case into a relative one.
    """
    with tarfile.open(path, "w:gz") as tar:
        for member in members:
            if member.isreg() and member.size:
                tar.addfile(member, io.BytesIO(b"x" * member.size))
            else:
                tar.addfile(member)
    return path


def _file(name: str, size: int = 4) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    return info


def _dir(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    return info


def _link(name: str, target: str, *, hard: bool) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.LNKTYPE if hard else tarfile.SYMTYPE
    info.linkname = target
    return info


def _extract_expecting_containment(extract, archive: Path, dest: Path) -> None:
    """Extract and require containment, however the implementation achieves it.

    The two paths legitimately differ in *mechanism*: the manual fallback
    refuses an unsafe member outright, while the stdlib ``data`` filter often
    neutralizes it instead — it strips a leading ``/``, and on POSIX a name like
    ``..\\outside`` is one legal filename component, so it lands inside as a
    literal file. Both satisfy the contract, which is containment, not raising.
    Asserting the mechanism would pin an implementation detail of CPython.
    """
    with tarfile.open(archive) as tar:
        try:
            extract(tar, dest)
        except tarfile.TarError:
            pass


def _nothing_outside(tmp_path: Path, dest: Path) -> None:
    """Assert the extraction wrote nothing outside ``dest``."""
    strays = [
        p
        for p in tmp_path.rglob("*")
        if p.is_file() and dest not in p.parents and p.suffix != ".gz"
    ]
    assert not strays, f"content escaped the destination: {strays}"


class TestEscapesAreBlocked:
    """Each case is a real bypass of the lexical check this replaced."""

    def test_traversal_name(self, tmp_path, extract):
        dest = tmp_path / "dest"
        dest.mkdir()
        archive = _tar_with(tmp_path / "t.tar.gz", _file("../escaped.txt"))
        _extract_expecting_containment(extract, archive, dest)
        _nothing_outside(tmp_path, dest)

    def test_absolute_name(self, tmp_path, extract):
        dest = tmp_path / "dest"
        dest.mkdir()
        # Absolute member names are expressed under tmp_path so the assertion
        # is about this run, not about global filesystem state.
        outside = tmp_path / "outside-abs.txt"
        archive = _tar_with(tmp_path / "t.tar.gz", _file(str(outside)))
        _extract_expecting_containment(extract, archive, dest)
        assert not outside.exists()
        _nothing_outside(tmp_path, dest)

    def test_symlink_target_escapes_and_a_later_member_writes_through_it(
        self, tmp_path, extract
    ):
        """Bypass #1: clean *name*, escaping *target*."""
        dest = tmp_path / "dest"
        dest.mkdir()
        (tmp_path / "outside").mkdir()
        payload = b"pwned\n"
        through = tarfile.TarInfo("snap/link/pwned.txt")
        through.size = len(payload)
        archive = _tar_with(
            tmp_path / "t.tar.gz",
            _dir("snap"),
            _link("snap/link", "../../outside", hard=False),
            through,
        )
        with tarfile.open(archive) as tar:
            with pytest.raises(tarfile.TarError):
                extract(tar, dest)
        assert not (tmp_path / "outside" / "pwned.txt").exists()
        _nothing_outside(tmp_path, dest)

    def test_hardlink_target_resolves_from_the_extraction_root(self, tmp_path, extract):
        """Bypass #2: hardlinks resolve from the root, not the link's parent.

        ``tarfile`` sets ``tarinfo._link_target = os.path.join(path, linkname)``
        with ``path`` the extraction root, so ``a/b/link -> ../victim`` reads as
        the harmless ``a/victim`` under the link's parent but lands beside the
        destination.
        """
        dest = tmp_path / "dest"
        dest.mkdir()
        victim = tmp_path / "victim.txt"
        victim.write_text("ORIGINAL\n")
        archive = _tar_with(
            tmp_path / "t.tar.gz",
            _dir("a"),
            _dir("a/b"),
            _link("a/b/link", "../victim.txt", hard=True),
        )
        with tarfile.open(archive) as tar:
            with pytest.raises(tarfile.TarError):
                extract(tar, dest)
        assert victim.read_text() == "ORIGINAL\n"

    def test_earlier_symlink_changes_what_a_later_path_means(self, tmp_path, extract):
        """Bypass #3, the one that retired the lexical check.

        With ``a -> .``, the member ``a/b/link`` has lexical depth 2 but real
        depth 1, so ``../../outside`` normalizes as contained while resolving
        outside. No check over member metadata can see this, because it depends
        on what an earlier member created.
        """
        dest = tmp_path / "dest"
        dest.mkdir()
        (tmp_path / "outside").mkdir()
        payload = b"PWNED\n"
        through = tarfile.TarInfo("a/b/link/pwned")
        through.size = len(payload)
        archive = _tar_with(
            tmp_path / "t.tar.gz",
            _link("a", ".", hard=False),
            _dir("a/b"),
            _link("a/b/link", "../../outside", hard=False),
            through,
        )
        with tarfile.open(archive) as tar:
            with pytest.raises(tarfile.TarError):
                extract(tar, dest)
        assert not (tmp_path / "outside" / "pwned").exists()
        _nothing_outside(tmp_path, dest)

    @pytest.mark.parametrize(
        "member_name",
        [
            r"..\outside.txt",
            r"skills\..\..\outside.txt",
            r"C:\outside.txt",
            r"\\server\share\outside.txt",
            r"C:outside.txt",
        ],
    )
    def test_windows_style_paths(self, tmp_path, member_name, extract):
        r"""`os.path` on Windows splits on `\` and honours drive letters, so
        these are escapes there while looking like one opaque POSIX component.
        The archive decides, not the host, so they are refused everywhere.
        """
        dest = tmp_path / "dest"
        dest.mkdir()
        archive = _tar_with(tmp_path / "t.tar.gz", _file(member_name))
        _extract_expecting_containment(extract, archive, dest)
        _nothing_outside(tmp_path, dest)

    @pytest.mark.parametrize(
        "member_type", [tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE]
    )
    def test_device_and_fifo_members(self, tmp_path, member_type, extract):
        dest = tmp_path / "dest"
        dest.mkdir()
        info = tarfile.TarInfo("snap/weird")
        info.type = member_type
        archive = _tar_with(tmp_path / "t.tar.gz", info)
        with tarfile.open(archive) as tar:
            with pytest.raises(tarfile.TarError):
                extract(tar, dest)


PRESERVED = frozenset({".hub", ".curator_backups"})


class TestDestinationStateCannotRedirect:
    """The destination is not a clean tree, and guarding it did not work.

    ``curator_backup.rollback()`` moves every top-level entry aside *except*
    ``.hub`` and ``.curator_backups``, so those two are all that extraction can
    collide with — and ``snapshot_skills()`` excludes both, so no legitimate
    archive contains a member under either.

    An earlier version tried to inspect the destination and detach hazards
    before handing over. It produced a P1 in six consecutive review rounds: a
    preserved hardlink truncated in place, a Windows junction walked through
    and unlinked, a symlink *member* redirecting a later member, a contained
    symlink already in the tree doing the same, a hardlink whose source was a
    symlink member. Each fix closed an instance and the class survived, for the
    reason round six already established — a pass over member metadata cannot
    know where a member lands.

    Refusing members under the preserved names closes the class instead:
    extraction only ever writes paths that do not exist yet, and an empty
    destination has no hardlink, junction or symlink to abuse. These tests
    assert the *property* — the outside file is untouched — for every redirect
    previously found.
    """

    def _hazard_setup(self, tmp_path):
        outside = tmp_path / "important.txt"
        outside.write_text("USER DATA\n")
        dest = tmp_path / "skills"
        (dest / ".hub").mkdir(parents=True)
        return outside, dest

    def _extract(self, extract, archive, dest):
        with tarfile.open(archive) as tar:
            with pytest.raises(tarfile.TarError):
                extract(tar, dest, refuse_top_level=PRESERVED)

    def test_a_preserved_hardlink_is_never_reached(self, tmp_path, extract):
        outside, dest = self._hazard_setup(tmp_path)
        os.link(outside, dest / ".hub" / "x")
        self._extract(extract, _tar_with(tmp_path / "t.tar.gz", _file(".hub/x", size=5)), dest)
        assert outside.read_text() == "USER DATA\n"
        assert outside.stat().st_nlink == 2

    def test_a_preserved_symlink_leaf_is_never_reached(self, tmp_path, extract):
        outside, dest = self._hazard_setup(tmp_path)
        (dest / ".hub" / "x").symlink_to(outside)
        self._extract(extract, _tar_with(tmp_path / "t.tar.gz", _file(".hub/x", size=5)), dest)
        assert outside.read_text() == "USER DATA\n"

    def test_a_contained_symlink_in_the_preserved_tree_is_never_reached(
        self, tmp_path, extract
    ):
        """``.hub/link -> sub`` with ``.hub/sub/x`` hardlinked outside."""
        outside, dest = self._hazard_setup(tmp_path)
        (dest / ".hub" / "sub").mkdir()
        os.link(outside, dest / ".hub" / "sub" / "x")
        (dest / ".hub" / "link").symlink_to("sub")
        self._extract(
            extract, _tar_with(tmp_path / "t.tar.gz", _file(".hub/link/x", size=5)), dest
        )
        assert outside.read_text() == "USER DATA\n"

    @pytest.mark.parametrize("spelling", [".hub/x", "/.hub/x", "./.hub/x", ".hub\\x"])
    def test_the_refusal_is_not_defeated_by_spelling(self, tmp_path, extract, spelling):
        """The refusal must not depend on how the member spells its path.

        ``data`` strips a leading slash, and ``tarfile`` builds its destination
        with ``os.path`` — so on Windows ``.hub\\x`` is two components while
        ``PurePosixPath`` sees one opaque name. Reading it only the POSIX way
        let that spelling walk past the preserved-name refusal.
        """
        outside, dest = self._hazard_setup(tmp_path)
        os.link(outside, dest / ".hub" / "x")
        self._extract(extract, _tar_with(tmp_path / "t.tar.gz", _file(spelling, size=5)), dest)
        assert outside.read_text() == "USER DATA\n"

    def test_a_symlink_member_cannot_redirect_into_the_preserved_tree(
        self, tmp_path, extract
    ):
        """``a -> .hub`` then ``a/x``: refused as a member under a symlink."""
        outside, dest = self._hazard_setup(tmp_path)
        os.link(outside, dest / ".hub" / "x")
        archive = _tar_with(
            tmp_path / "t.tar.gz", _link("a", ".hub", hard=False), _file("a/x", size=5)
        )
        self._extract(extract, archive, dest)
        assert outside.read_text() == "USER DATA\n"

    def test_a_hardlink_whose_source_is_a_symlink_member_is_refused(
        self, tmp_path, extract
    ):
        """``a -> .hub/x``, hardlink ``b -> a``, then a regular ``b``.

        "An earlier member" was not a strong enough rule: the alias made ``b``
        a symlink that the final member followed. The source must be an earlier
        *regular file* member.
        """
        outside, dest = self._hazard_setup(tmp_path)
        os.link(outside, dest / ".hub" / "x")
        archive = _tar_with(
            tmp_path / "t.tar.gz",
            _link("a", ".hub/x", hard=False),
            _link("b", "a", hard=True),
            _file("b", size=5),
        )
        self._extract(extract, archive, dest)
        assert outside.read_text() == "USER DATA\n"

    def test_an_ordinary_archive_still_extracts_into_a_live_destination(
        self, tmp_path, extract
    ):
        """The control: refusing must not mean refusing everything."""
        _, dest = self._hazard_setup(tmp_path)
        (dest / "old.md").write_text("stale")
        archive = _tar_with(
            tmp_path / "t.tar.gz", _dir("demo"), _file("demo/SKILL.md", size=7)
        )
        with tarfile.open(archive) as tar:
            extract(tar, dest, refuse_top_level=PRESERVED)
        assert (dest / "demo" / "SKILL.md").read_bytes() == b"x" * 7
        assert (dest / ".hub").is_dir()


class TestHardlinksInRealSnapshots:
    """``tarfile.add()`` stores a repeated inode as a LNKTYPE member.

    A skills tree containing hardlinks therefore produces a snapshot with link
    members through no fault of the archive's author, and refusing those would
    make ``snapshot_skills()`` output unrestorable on the very interpreters
    this fallback exists for.
    """

    def test_snapshot_with_hardlinks_round_trips(self, tmp_path, extract):
        skills = tmp_path / "skills" / "demo"
        skills.mkdir(parents=True)
        (skills / "a.txt").write_text("content\n")
        os.link(skills / "a.txt", skills / "b.txt")

        archive = tmp_path / "snap.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for entry in sorted((tmp_path / "skills").iterdir()):
                tar.add(str(entry), arcname=entry.name, recursive=True)
        with tarfile.open(archive) as tar:
            assert any(m.islnk() for m in tar.getmembers()), "fixture must contain a hardlink"

        dest = tmp_path / "dest"
        dest.mkdir()
        with tarfile.open(archive) as tar:
            extract(tar, dest)

        a, b = dest / "demo" / "a.txt", dest / "demo" / "b.txt"
        assert a.read_text() == "content\n"
        assert b.read_text() == "content\n", "hardlinked sibling was not restored"

    def test_hardlink_is_materialized_as_a_copy(self, tmp_path, monkeypatch):
        """A copy has the same content but cannot reach anything else."""
        skills = tmp_path / "skills" / "demo"
        skills.mkdir(parents=True)
        (skills / "a.txt").write_text("content\n")
        os.link(skills / "a.txt", skills / "b.txt")
        archive = tmp_path / "snap.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for entry in sorted((tmp_path / "skills").iterdir()):
                tar.add(str(entry), arcname=entry.name, recursive=True)

        real = tarfile.TarFile.extractall

        def no_filter(self, path=".", members=None, **kwargs):
            if "filter" in kwargs:
                raise TypeError("no filter")
            return real(self, path, members, **kwargs)

        monkeypatch.setattr(tarfile.TarFile, "extractall", no_filter)
        dest = tmp_path / "dest"
        dest.mkdir()
        with tarfile.open(archive) as tar:
            safe_extract_tar(tar, dest)
        a, b = dest / "demo" / "a.txt", dest / "demo" / "b.txt"
        assert a.stat().st_ino != b.stat().st_ino


class TestPlatformsWithoutDirFdAreRefused:
    """No enforcement means no extraction, not extraction without enforcement.

    Windows has neither ``O_NOFOLLOW`` nor ``dir_fd`` support, so the
    component walk cannot run there. An earlier version fell back to plain path
    operations and documented the hole; that fallback would follow a directory
    junction already present in the destination — the very redirect the walk
    exists to refuse.
    """

    def test_extraction_is_refused_without_dir_fd(self, tmp_path, monkeypatch):
        monkeypatch.setattr(file_safety, "_HAVE_DIR_FD", False)
        real = tarfile.TarFile.extractall

        def no_filter(self, path=".", members=None, **kwargs):
            if "filter" in kwargs:
                raise TypeError("no filter")
            return real(self, path, members, **kwargs)

        monkeypatch.setattr(tarfile.TarFile, "extractall", no_filter)

        dest = tmp_path / "dest"
        dest.mkdir()
        archive = _tar_with(
            tmp_path / "t.tar.gz", _dir("skills"), _file("skills/a.md", size=3)
        )
        with tarfile.open(archive) as tar:
            with pytest.raises(tarfile.TarError, match="3.11.4"):
                safe_extract_tar(tar, dest)
        assert not (dest / "skills" / "a.md").exists(), "wrote without enforcement"

    def test_the_filtered_path_is_unaffected(self, tmp_path, monkeypatch):
        """Only the fallback is refused — 3.11.4+ never reaches the walk."""
        monkeypatch.setattr(file_safety, "_HAVE_DIR_FD", False)
        dest = tmp_path / "dest"
        dest.mkdir()
        archive = _tar_with(
            tmp_path / "t.tar.gz", _dir("skills"), _file("skills/a.md", size=3)
        )
        with tarfile.open(archive) as tar:
            safe_extract_tar(tar, dest)
        assert (dest / "skills" / "a.md").is_file()


class TestFileModesMatchTheStdlib:
    """Regular-file modes are sanitized exactly as ``filter="data"`` does.

    Unlike directories — which ``data`` ignores — it *does* sanitize regular
    files: drop group/other write, clear all execute bits unless the owner had
    execute, then guarantee owner read/write. Applying the archived mode
    verbatim restored a 0777 member world-writable on the fallback and 0755
    everywhere else.
    """

    @pytest.mark.parametrize(
        ("archived", "expected"),
        [(0o777, 0o755), (0o666, 0o644), (0o755, 0o755),
         (0o600, 0o600), (0o400, 0o600), (0o444, 0o644)],
    )
    def test_mode_is_sanitized(self, tmp_path, extract, archived, expected):
        dest = tmp_path / "dest"
        dest.mkdir()
        info = _file("skills/a.md", size=3)
        info.mode = archived
        archive = _tar_with(tmp_path / "t.tar.gz", _dir("skills"), info)
        with tarfile.open(archive) as tar:
            extract(tar, dest)
        got = (dest / "skills" / "a.md").stat().st_mode & 0o777
        assert got == expected, f"{archived:04o} -> {got:04o}, expected {expected:04o}"

    def test_helper_matches_the_stdlib_for_every_mode(self):
        """Exhaustive rather than sampled — the rule is only 3 lines."""
        for mode in range(0o1000):
            sanitized = file_safety._data_filter_mode(mode)
            expected = mode & 0o755
            if not expected & 0o100:
                expected &= ~0o111
            expected |= 0o600
            assert sanitized == expected, f"{mode:04o}"
            assert not sanitized & 0o022, f"{mode:04o} left group/other write"
            assert sanitized & 0o600 == 0o600, f"{mode:04o} lost owner rw"


class TestDirectoryModesMatchTheStdlib:
    """The two paths must agree on directory permissions.

    ``filter="data"`` does **not** restore a directory's archived mode: it
    creates the directory with ``os.mkdir``'s default 0777 and lets the
    process umask decide. So the fallback must not restore it either — but it
    must also not *hard-code* the result.

    An earlier version of this test asserted a bare ``0o755``, and the
    fallback hard-coded ``os.mkdir(part, 0o755)`` to match. Both were only
    right under umask 022, where ``0777 & ~022`` happens to be 0755. Under
    umask 002 the stdlib yields 0775 and the fallback still gave 0755, so a
    group-shared skills tree silently lost group write depending on the Python
    patch version. The expectation is therefore computed from the umask rather
    than written down.
    """

    @staticmethod
    def _expected(umask: int) -> int:
        return 0o777 & ~umask

    def _extract_private_dir(self, tmp_path, extract, dest_name):
        dest = tmp_path / dest_name
        dest.mkdir()
        private = _dir("skills/private")
        private.mode = 0o700
        archive = _tar_with(
            tmp_path / f"{dest_name}.tar.gz",
            _dir("skills"),
            private,
            _file("skills/private/secret.md", size=5),
        )
        with tarfile.open(archive) as tar:
            extract(tar, dest)
        assert (dest / "skills" / "private" / "secret.md").is_file()
        return (dest / "skills" / "private").stat().st_mode & 0o777

    @pytest.mark.skipif(os.name == "nt", reason="POSIX umask semantics")
    @pytest.mark.parametrize("umask", [0o022, 0o002, 0o000, 0o077])
    def test_both_paths_agree_under_any_umask(self, tmp_path, extract, umask):
        """Whatever the mode ends up being, both paths must produce the same.

        Parameterized over both extraction paths *and* several umasks, so a
        drift in either direction fails here — including the one a single
        umask cannot see.
        """
        previous = os.umask(umask)
        try:
            mode = self._extract_private_dir(tmp_path, extract, "dest")
        finally:
            os.umask(previous)
        assert mode == self._expected(umask), (
            f"under umask {umask:04o} expected parity with filter='data' "
            f"({self._expected(umask):04o}), got {mode:04o}"
        )


class TestOrdinaryArchivesStillExtract:
    """The guard must not be safe by refusing everything."""

    def test_files_and_directories_round_trip(self, tmp_path, extract):
        dest = tmp_path / "dest"
        dest.mkdir()
        archive = _tar_with(
            tmp_path / "t.tar.gz",
            _dir("skills"),
            _dir("skills/demo"),
            _file("skills/demo/SKILL.md", size=11),
        )
        with tarfile.open(archive) as tar:
            extract(tar, dest)
        written = dest / "skills" / "demo" / "SKILL.md"
        assert written.is_file()
        assert written.read_bytes() == b"x" * 11

    def test_mode_is_preserved(self, tmp_path, extract):
        dest = tmp_path / "dest"
        dest.mkdir()
        info = _file("skills/run.sh", size=3)
        info.mode = 0o755
        archive = _tar_with(tmp_path / "t.tar.gz", _dir("skills"), info)
        with tarfile.open(archive) as tar:
            extract(tar, dest)
        assert (dest / "skills" / "run.sh").stat().st_mode & 0o777 == 0o755

    def test_a_real_archive_of_repo_content(self, tmp_path, extract):
        """Built the way ``snapshot_skills()`` builds one, from real files."""
        src = Path(__file__).resolve().parents[2] / "docs"
        if not src.is_dir():
            pytest.skip("docs/ not present")
        archive = tmp_path / "real.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for entry in sorted(src.iterdir()):
                tar.add(str(entry), arcname=entry.name, recursive=True)
        dest = tmp_path / "dest"
        dest.mkdir()
        with tarfile.open(archive) as tar:
            extract(tar, dest)
        assert sum(1 for p in dest.rglob("*") if p.is_file()) > 0


class TestLiteralBackslashNamesMatchTheStdlib:
    r"""A backslash is a legal POSIX filename character, not a separator.

    Refusing Windows-style *escapes* is right — ``..\outside.txt`` is pinned
    above. But the fallback implemented that by rewriting every ``\`` to ``/``
    before splitting, which is not a refusal at all: it silently relocated the
    file. ``demo/a\b`` and ``demo/a/b`` both resolved to ``demo/a/b``, so one
    snapshot entry overwrote the other and a rollback lost a file without
    saying so.

    The stdlib is the oracle here, as everywhere else in this module: on POSIX
    ``filter="data"`` keeps the two distinct, so the fallback must too.
    """

    @pytest.mark.skipif(os.sep != "/", reason="POSIX-only filename semantics")
    def test_a_backslash_name_is_not_a_directory_separator(self, tmp_path, extract):
        dest = tmp_path / "dest"
        dest.mkdir()
        archive = _tar_with(
            tmp_path / "t.tar.gz",
            _dir("demo"),
            _file(r"demo/a\b", size=5),
        )
        with tarfile.open(archive) as tar:
            extract(tar, dest)
        assert (dest / "demo" / "a\\b").is_file()
        assert not (dest / "demo" / "a").is_dir()

    @pytest.mark.skipif(os.sep != "/", reason="POSIX-only filename semantics")
    def test_a_backslash_name_does_not_collide_with_a_nested_one(self, tmp_path, extract):
        """The data-loss case: two distinct entries, two distinct files."""
        dest = tmp_path / "dest"
        dest.mkdir()
        archive = _tar_with(
            tmp_path / "t.tar.gz",
            _dir("demo"),
            _file(r"demo/a\b", size=5),
            _dir("demo/a"),
            _file("demo/a/b", size=9),
        )
        with tarfile.open(archive) as tar:
            extract(tar, dest)
        assert (dest / "demo" / "a\\b").stat().st_size == 5
        assert (dest / "demo" / "a" / "b").stat().st_size == 9

    @pytest.mark.skipif(os.sep != "/", reason="POSIX-only filename semantics")
    def test_escapes_written_with_backslashes_are_still_refused(self, tmp_path, extract):
        r"""Preserving a literal ``\`` must not reopen the Windows escapes."""
        dest = tmp_path / "dest"
        dest.mkdir()
        archive = _tar_with(tmp_path / "t.tar.gz", _file(r"..\outside.txt"))
        _extract_expecting_containment(extract, archive, dest)
        _nothing_outside(tmp_path, dest)


class TestContainedSymlinksRoundTrip:
    """Both paths restore a contained symlink, and neither restores an escape.

    The fallback used to refuse *every* link member, which was the same
    "legitimate snapshot cannot be restored" failure as the hardlink case —
    ``agent/skill_utils.py`` supports symlinked skills, so a snapshot of one
    would not come back on 3.11.0–3.11.3.

    Two mechanisms make restoring them safe. The load-bearing one is that the
    component walk refuses to traverse *any* symlink, including one this
    extraction just created, so nothing is written through a link whenever it
    is created. Creating links last is defence in depth on top of that —
    checked by making creation inline, where every escape case below still
    fails. Containment is then confirmed with ``realpath`` over the finished
    tree, which resolves through whatever chain the archive built.
    """

    def test_contained_symlink_is_restored(self, tmp_path, extract):
        dest = tmp_path / "dest"
        dest.mkdir()
        archive = _tar_with(
            tmp_path / "t.tar.gz",
            _dir("demo"),
            _file("demo/REAL", size=3),
            _link("demo/README", "REAL", hard=False),
        )
        with tarfile.open(archive) as tar:
            extract(tar, dest)
        link = dest / "demo" / "README"
        assert link.is_symlink()
        assert link.read_bytes() == b"xxx", "link does not resolve to its target"

    def test_escaping_symlink_is_refused_and_not_left_behind(self, tmp_path, extract):
        dest = tmp_path / "dest"
        dest.mkdir()
        archive = _tar_with(
            tmp_path / "t.tar.gz",
            _dir("demo"),
            _link("demo/bad", "../../../../etc/passwd", hard=False),
        )
        with tarfile.open(archive) as tar:
            with pytest.raises(tarfile.TarError):
                extract(tar, dest)
        assert not (dest / "demo" / "bad").is_symlink(), "failed restore left a link"

    def test_a_link_chain_that_escapes_is_caught_by_realpath(self, tmp_path, extract):
        """Lexically each hop is contained; only resolution shows the escape."""
        dest = tmp_path / "dest"
        dest.mkdir()
        archive = _tar_with(
            tmp_path / "t.tar.gz",
            _dir("a"),
            _dir("a/b"),
            _link("a/b/up", "../..", hard=False),
            _link("a/b/out", "up/../../outside", hard=False),
        )
        with tarfile.open(archive) as tar:
            with pytest.raises(tarfile.TarError):
                extract(tar, dest)
        _nothing_outside(tmp_path, dest)


class TestMtimesMatchTheStdlib:
    """``data`` preserves archived mtimes; the fallback must too.

    ``build_skill_nodes()`` falls back to ``SKILL.md``'s mtime for skills with
    no usage record, so a restore that stamps "now" changes behaviour rather
    than just metadata.
    """

    @pytest.mark.parametrize("kind", ["file", "dir", "hardlink"])
    def test_a_fractional_mtime_is_not_truncated(self, tmp_path, extract, kind):
        """PAX stores mtime as a float, and ``data`` restores the fraction.

        The fallback coerced it with ``int()`` at all three write sites, so a
        rollback's timestamps depended on the interpreter and two skills
        touched within the same second collapsed to the same time — which is
        exactly the ordering ``build_skill_nodes()`` reads.
        """
        archived = 946684800.75
        info = _file("demo/f.txt", size=3)
        info.mtime = archived
        parent = _dir("demo")
        parent.mtime = archived
        link = _link("demo/hard.txt", "demo/f.txt", hard=True)
        link.mtime = archived

        members = {"file": (parent, info), "dir": (parent, info),
                   "hardlink": (parent, info, link)}[kind]
        archive = tmp_path / "t.tar.gz"
        with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as tar:
            for member in members:
                if member.isreg() and member.size:
                    tar.addfile(member, io.BytesIO(b"x" * member.size))
                else:
                    tar.addfile(member)

        dest = tmp_path / "dest"
        dest.mkdir()
        with tarfile.open(archive) as tar:
            extract(tar, dest)

        target = {"file": dest / "demo" / "f.txt", "dir": dest / "demo",
                  "hardlink": dest / "demo" / "hard.txt"}[kind]
        assert target.stat().st_mtime == pytest.approx(archived, abs=1e-6)

    def test_a_hardlinked_sibling_keeps_its_mtime(self, tmp_path, extract):
        """The copy must carry the archived time, like the regular-file path.

        tarfile.add() stores a repeated inode as LNKTYPE, so in a real snapshot
        the second of two hardlinked files takes the copy path — and would
        otherwise restore stamped "now" while its twin kept 2000-01-01.
        """
        archived = 946684800
        skills = tmp_path / "skills" / "demo"
        skills.mkdir(parents=True)
        (skills / "a.txt").write_text("content\n")
        os.link(skills / "a.txt", skills / "b.txt")
        os.utime(skills / "a.txt", (archived, archived))

        archive = tmp_path / "snap.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for entry in sorted((tmp_path / "skills").iterdir()):
                tar.add(str(entry), arcname=entry.name, recursive=True)
        with tarfile.open(archive) as tar:
            assert any(m.islnk() for m in tar.getmembers())

        dest = tmp_path / "dest"
        dest.mkdir()
        with tarfile.open(archive) as tar:
            extract(tar, dest)
        for name in ("a.txt", "b.txt"):
            got = int((dest / "demo" / name).stat().st_mtime)
            assert got == archived, f"{name} restored with mtime {got}"

    def test_file_and_directory_mtimes_are_preserved(self, tmp_path, extract):
        archived = 946684800  # 2000-01-01
        dest = tmp_path / "dest"
        dest.mkdir()
        d = _dir("skills")
        d.mtime = archived
        f = _file("skills/SKILL.md", size=3)
        f.mtime = archived
        archive = _tar_with(tmp_path / "t.tar.gz", d, f)
        with tarfile.open(archive) as tar:
            extract(tar, dest)
        assert int((dest / "skills" / "SKILL.md").stat().st_mtime) == archived
        assert int((dest / "skills").stat().st_mtime) == archived, (
            "directory mtime was clobbered by writing its children"
        )


class TestOutOfRangeTimestamps:
    """A timestamp must never be able to abort a restore.

    A PAX member can carry a numeric mtime that ``os.utime`` cannot represent.
    That raised ``OverflowError`` straight out of ``safe_extract_tar`` — and
    because it is not a ``TarError``, ``rollback()`` skipped its
    extraction-failure recovery, leaving the partial tree in place and the
    original still staged. Metadata failing is not extraction failing.
    """

    # NaN is deliberately absent: `tarfile` cannot even write it
    # ("cannot convert float NaN to integer"), so an archive carrying one does
    # not exist. `_representable_mtime` still checks `isfinite`, but a test for
    # an unreachable input would only assert a fiction.
    @pytest.mark.parametrize("mtime", [1e300, -1e300])
    def test_an_unrepresentable_mtime_fails_recoverably(self, tmp_path, extract, mtime):
        """The contract is the *exception type*, not that extraction succeeds.

        `os.utime` raises `OverflowError`, and `extractall` passes it straight
        through. Callers catch `TarError`, so a non-TarError escaping here is
        what makes `rollback()` skip recovery. Both paths must therefore raise
        `TarError` — the stdlib path cannot be made to tolerate the value from
        outside, so it is rejected before extraction starts.
        """
        dest = tmp_path / "dest"
        dest.mkdir()
        info = _file("skills/a.txt", size=3)
        info.mtime = mtime
        directory = _dir("skills")
        archive = tmp_path / "t.tar.gz"
        with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as tar:
            tar.addfile(directory)
            tar.addfile(info, io.BytesIO(b"xxx"))
        with tarfile.open(archive) as tar:
            with pytest.raises(tarfile.TarError):
                extract(tar, dest)

    def test_an_ordinary_mtime_is_unaffected(self, tmp_path, extract):
        """The control: the bound must not reject a real timestamp."""
        dest = tmp_path / "dest"
        dest.mkdir()
        info = _file("skills/a.txt", size=3)
        info.mtime = 946684800
        archive = tmp_path / "t.tar.gz"
        with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as tar:
            tar.addfile(_dir("skills"))
            tar.addfile(info, io.BytesIO(b"xxx"))
        with tarfile.open(archive) as tar:
            extract(tar, dest)
        assert (dest / "skills" / "a.txt").stat().st_mtime == 946684800


def test_a_directory_timestamp_is_validated_too(tmp_path, extract):
    """`data` applies directory attributes last, so this escaped every check.

    The validator short-circuited on directory members before reaching the
    range check, and `extractall` then raised `OverflowError` from the very
    end of extraction — not a `TarError`, so `rollback()` skipped recovery
    with the tree already staged.
    """
    dest = tmp_path / "dest"
    dest.mkdir()
    directory = _dir("skills")
    directory.mtime = 1e300
    info = _file("skills/a.txt", size=1)
    archive = tmp_path / "t.tar.gz"
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as tar:
        tar.addfile(directory)
        tar.addfile(info, io.BytesIO(b"x"))
    with tarfile.open(archive) as tar:
        with pytest.raises(tarfile.TarError):
            extract(tar, dest)


class TestDirectoryMembersAreValidatedToo:
    """Directories short-circuited the validator and skipped every check."""

    def test_a_directory_inside_the_preserved_tree_is_refused(self, tmp_path, extract):
        """`.hub/injected` was created, and rollback reported success.

        Nothing was written *through* it, so none of the redirect tests caught
        it — but curator rollback is not supposed to touch hub-managed state at
        all, and a directory member is enough to mutate it.
        """
        dest = tmp_path / "skills"
        (dest / ".hub").mkdir(parents=True)
        directory = _dir(".hub/injected")
        archive = _tar_with(tmp_path / "t.tar.gz", directory)
        with tarfile.open(archive) as tar:
            with pytest.raises(tarfile.TarError):
                extract(tar, dest, refuse_top_level=PRESERVED)
        assert not (dest / ".hub" / "injected").exists()


class TestDuplicateMemberNames:
    """A name written twice invalidates anything concluded about it."""

    def test_a_replaced_member_cannot_launder_a_hardlink(self, tmp_path, extract):
        """Regular ``a``, then a symlink also named ``a``, then ``b -> a``.

        ``a`` was a regular file when the hardlink was validated and a symlink
        by the time extraction used it, so a source-type check alone could not
        hold. Refusing duplicates removes the possibility rather than tracking
        provenance through replacement.
        """
        outside = tmp_path / "important.txt"
        outside.write_text("USER DATA\n")
        dest = tmp_path / "skills"
        (dest / ".hub").mkdir(parents=True)
        os.link(outside, dest / ".hub" / "x")
        archive = _tar_with(
            tmp_path / "t.tar.gz",
            _file("a", size=1),
            _link("a", ".hub/x", hard=False),
            _link("b", "a", hard=True),
            _file("b", size=5),
        )
        with tarfile.open(archive) as tar:
            with pytest.raises(tarfile.TarError):
                extract(tar, dest, refuse_top_level=PRESERVED)
        assert outside.read_text() == "USER DATA\n"

    def test_an_archive_without_duplicates_is_unaffected(self, tmp_path, extract):
        """The control — and `test_a_real_archive_of_repo_content` covers the
        same ground against a tarball built from real files, which is what
        would catch this rule being too strict for ordinary snapshots."""
        dest = tmp_path / "dest"
        dest.mkdir()
        archive = _tar_with(
            tmp_path / "t.tar.gz",
            _dir("skills"),
            _file("skills/a.md", size=3),
            _file("skills/b.md", size=3),
        )
        with tarfile.open(archive) as tar:
            extract(tar, dest)
        assert (dest / "skills" / "a.md").is_file()
        assert (dest / "skills" / "b.md").is_file()


class TestSpellingsThatEvadeThePreservedNameCheck:
    """The refusal must not depend on how a member spells a preserved path."""

    def _hazard(self, tmp_path):
        outside = tmp_path / "important.txt"
        outside.write_text("USER DATA\n")
        dest = tmp_path / "skills"
        (dest / ".hub").mkdir(parents=True)
        os.link(outside, dest / ".hub" / "x")
        return outside, dest

    @pytest.mark.parametrize("name", [".HUB/x", ".Hub/x"])
    def test_a_differently_cased_preserved_name(self, tmp_path, extract, name):
        """Windows and default macOS resolve `.HUB/x` onto the existing `.hub`.

        Folding unconditionally can only over-refuse, and only for an archive
        containing a differently-cased `.hub` — not a skill name.
        """
        outside, dest = self._hazard(tmp_path)
        archive = _tar_with(tmp_path / "t.tar.gz", _file(name, size=5))
        with tarfile.open(archive) as tar:
            with pytest.raises(tarfile.TarError):
                extract(tar, dest, refuse_top_level=PRESERVED)
        assert outside.read_text() == "USER DATA\n"

    def test_a_windows_spelled_symlink_ancestor(self, tmp_path, extract):
        r"""``a\link -> ..\.hub`` then ``a\link\x``.

        Recorded as the single component ``a\link``, which is not a prefix of
        the single component ``a\link\x``, so the redirect went unseen. Both
        separator readings are tracked now.
        """
        outside, dest = self._hazard(tmp_path)
        archive = _tar_with(
            tmp_path / "t.tar.gz",
            _link("a\\link", "..\\.hub", hard=False),
            _file("a\\link\\x", size=5),
        )
        with tarfile.open(archive) as tar:
            with pytest.raises(tarfile.TarError):
                extract(tar, dest, refuse_top_level=PRESERVED)
        assert outside.read_text() == "USER DATA\n"


def test_an_embedded_nul_in_a_long_path_is_refused(tmp_path, extract):
    """`ValueError: embedded null byte` is neither OSError nor TarError.

    `rollback()` catches only those two, so it escaped recovery with the tree
    already staged. A short name would truncate at the NUL — it survives only
    via PAX's length-prefixed long-path record, which is how this is built.
    """
    dest = tmp_path / "dest"
    dest.mkdir()
    info = _file("demo/" + "a" * 120 + "\x00" + "b", size=1)
    archive = tmp_path / "t.tar.gz"
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as tar:
        tar.addfile(info, io.BytesIO(b"x"))
    with tarfile.open(archive) as tar:
        assert "\x00" in tar.getmembers()[0].name  # the premise
        with pytest.raises(tarfile.TarError):
            extract(tar, dest)


class TestNormalizationIsAppliedUniformly:
    """Three rounds running found the same defect: a rule applied in one check
    and not in the one beside it. These pin the rules the last two rounds added
    against the member kinds and spellings that had slipped past them.
    """

    def _hazard(self, tmp_path):
        outside = tmp_path / "important.txt"
        outside.write_text("USER DATA\n")
        dest = tmp_path / "skills"
        (dest / ".hub").mkdir(parents=True)
        os.link(outside, dest / ".hub" / "x")
        return outside, dest

    def test_a_directory_under_a_symlink_member(self, tmp_path, extract):
        """`a -> .hub` then directory `a/injected` — the third member kind to
        short-circuit past a check that was never type-specific."""
        outside, dest = self._hazard(tmp_path)
        archive = _tar_with(
            tmp_path / "t.tar.gz", _link("a", ".hub", hard=False), _dir("a/injected")
        )
        with tarfile.open(archive) as tar:
            with pytest.raises(tarfile.TarError):
                extract(tar, dest, refuse_top_level=PRESERVED)
        assert not (dest / ".hub" / "injected").exists()

    def test_a_case_differing_symlink_ancestor(self, tmp_path, extract):
        """`A -> .hub` then `a/x`: the same filesystem entry where case is
        insensitive, but `('A',)` is not a prefix of `('a','x')`."""
        outside, dest = self._hazard(tmp_path)
        archive = _tar_with(
            tmp_path / "t.tar.gz", _link("A", ".hub", hard=False), _file("a/x", size=5)
        )
        with tarfile.open(archive) as tar:
            with pytest.raises(tarfile.TarError):
                extract(tar, dest, refuse_top_level=PRESERVED)
        assert outside.read_text() == "USER DATA\n"

    def test_a_nul_in_a_symlink_target(self, tmp_path, extract):
        """`os.symlink` raises ValueError, which escapes rollback's recovery."""
        dest = tmp_path / "dest"
        dest.mkdir()
        info = _link("demo/l", "inside", hard=False)
        info.pax_headers = {"linkpath": "inside\x00evil"}
        archive = tmp_path / "t.tar.gz"
        with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as tar:
            tar.addfile(info)
        with tarfile.open(archive) as tar:
            with pytest.raises(tarfile.TarError):
                extract(tar, dest)

    def test_identity_still_uses_the_host_semantics(self, tmp_path, extract):
        """The control that caught this being over-applied.

        Refusal rules consider every reading, because over-refusing is cheap.
        *Identity* rules must not: judging duplicates across both separator
        readings made `demo/a\\b` and `demo/a/b` collide, and on POSIX those are
        two legitimate, different files.
        """
        dest = tmp_path / "dest"
        dest.mkdir()
        archive = _tar_with(
            tmp_path / "t.tar.gz",
            _dir("demo"),
            _file("demo/a\\b", size=2),
            _dir("demo/a"),
            _file("demo/a/b", size=3),
        )
        with tarfile.open(archive) as tar:
            extract(tar, dest)

    def test_a_case_differing_symlink_invalidates_hardlink_provenance(
        self, tmp_path, extract
    ):
        """Regular `demo/A`, symlink `demo/a -> ../.hub/x`, hardlink to `demo/A`.

        `os.path.normcase` is the identity function on macOS while the default
        filesystem there is case-insensitive, so host-keyed provenance saw two
        names where that filesystem has one entry: the symlink replaced the
        regular file, and the hardlink then inherited a source that was no
        longer a file.

        Invalidation is a *refusal* question, so it folds. On Linux the three
        members really are distinct and this archive is legitimate — refusing
        it is the deliberate cost, and this test pins that direction.
        """
        outside, dest = self._hazard(tmp_path)
        archive = _tar_with(
            tmp_path / "t.tar.gz",
            _dir("demo"),
            _file("demo/A", size=3),
            _link("demo/a", "../.hub/x", hard=False),
            _link("demo/b", "demo/A", hard=True),
        )
        with tarfile.open(archive) as tar:
            with pytest.raises(tarfile.TarError):
                extract(tar, dest, refuse_top_level=PRESERVED)
        assert outside.read_text() == "USER DATA\n"

    def test_hardlink_provenance_survives_an_unrelated_member(
        self, tmp_path, extract
    ):
        """The control: folding invalidation must not refuse ordinary archives.

        A regular file, an unrelated symlink beside it, then a hardlink to the
        file. Nothing here aliases anything, so the hardlink still resolves.
        """
        dest = tmp_path / "dest"
        dest.mkdir()
        archive = _tar_with(
            tmp_path / "t.tar.gz",
            _dir("demo"),
            _file("demo/a", size=3),
            _link("demo/link", "a", hard=False),
            _link("demo/b", "demo/a", hard=True),
        )
        with tarfile.open(archive) as tar:
            extract(tar, dest)
        assert (dest / "demo" / "b").is_file()
        assert (dest / "demo" / "link").is_symlink()

    def test_a_unicode_differing_symlink_ancestor(self, tmp_path, extract):
        """``é -> .hub`` composed, then ``é/x`` decomposed.

        macOS is normalization-*insensitive*: U+00E9 and U+0065 U+0301 name one
        file there, so the member resolves through the symlink while the two
        component tuples compare as different. Casefolding alone is not
        filesystem identity.
        """
        outside, dest = self._hazard(tmp_path)
        archive = _tar_with(
            tmp_path / "t.tar.gz",
            _link(NFC, ".hub", hard=False),
            _file(NFD + "/x", size=5),
        )
        with tarfile.open(archive) as tar:
            with pytest.raises(tarfile.TarError):
                extract(tar, dest, refuse_top_level=PRESERVED)
        assert outside.read_text() == "USER DATA\n"

    def test_unicode_identity_still_uses_the_host_semantics(self, tmp_path, extract):
        """The control for the rule above, and the same distinction again.

        Linux stores the two spellings as two files. Folding them for
        *identity* would call this legitimate archive a duplicate-destination
        collision and refuse it. The refusal side folds; this side does not.
        """
        dest = tmp_path / "dest"
        dest.mkdir()
        archive = _tar_with(
            tmp_path / "t.tar.gz",
            _dir("demo"),
            _file("demo/" + NFC, size=2),
            _file("demo/" + NFD, size=3),
        )
        with tarfile.open(archive) as tar:
            extract(tar, dest)
        assert (dest / "demo" / NFC).is_file()
        assert (dest / "demo" / NFD).is_file()
