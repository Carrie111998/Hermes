from pathlib import Path

import pytest

from hermes_cli.release_markers import stamp_release_marker
from scripts.release_snapshot import build_snapshot


def test_stamp_release_marker_requires_canonical_full_sha(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()

    marker = stamp_release_marker(release, "A" * 40)

    assert marker.read_text(encoding="utf-8") == ("a" * 40) + "\n"


@pytest.mark.parametrize("source_sha", ["abc", "g" * 40, "a" * 41])
def test_stamp_release_marker_rejects_noncanonical_sha(
    tmp_path: Path, source_sha: str
) -> None:
    release = tmp_path / "release"
    release.mkdir()

    with pytest.raises(ValueError, match="full 40-character"):
        stamp_release_marker(release, source_sha)


def test_build_snapshot_copies_source_and_refuses_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / "payload.txt").write_text("payload\n", encoding="utf-8")
    source_sha = "b" * 40

    result = build_snapshot(source, destination, source_sha)

    assert result == destination
    assert (destination / "payload.txt").read_text(encoding="utf-8") == "payload\n"
    assert (destination / ".hermes-release-sha").read_text(encoding="utf-8") == (
        source_sha + "\n"
    )
    with pytest.raises(FileExistsError):
        build_snapshot(source, destination, source_sha)