"""Integrity checks for the vendored brace-expansion compatibility package."""

from __future__ import annotations

import tarfile
from pathlib import Path, PurePosixPath


REPO_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_ROOT = REPO_ROOT / "vendor" / "brace-expansion-compat"
ADAPTER_ARCHIVE = ADAPTER_ROOT / "brace-expansion-compat-5.0.8-hermes.2.tgz"


def test_adapter_archive_exactly_matches_reviewable_sources() -> None:
    """The installed tarball must contain exactly the checked-in source bytes."""
    source_files = {
        path.relative_to(ADAPTER_ROOT).as_posix(): path
        for path in ADAPTER_ROOT.rglob("*")
        if path.is_file() and path != ADAPTER_ARCHIVE
    }

    with tarfile.open(ADAPTER_ARCHIVE, "r:gz") as archive:
        all_members = archive.getmembers()
        assert all(member.isfile() for member in all_members), (
            "adapter archive may contain regular files only"
        )

        member_names = [member.name for member in all_members]
        assert len(member_names) == len(set(member_names)), (
            "adapter archive contains duplicate member names"
        )

        for member_name in member_names:
            member_path = PurePosixPath(member_name)
            assert not member_path.is_absolute()
            assert member_path.parts[0] == "package"
            assert ".." not in member_path.parts

        members = {
            PurePosixPath(member.name).relative_to("package").as_posix(): member
            for member in all_members
        }

        assert set(members) == set(source_files)
        for relative_path, member in members.items():
            extracted = archive.extractfile(member)
            assert extracted is not None
            assert extracted.read() == source_files[relative_path].read_bytes(), (
                f"{relative_path} in the install archive differs from its "
                "reviewable source"
            )
