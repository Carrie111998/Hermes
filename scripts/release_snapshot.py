"""Build an isolated Hermes release snapshot with a canonical identity marker.

The DGX deployer can call ``build_snapshot`` and then install the returned
directory. The operation refuses to overwrite an existing snapshot.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

from hermes_cli.release_markers import stamp_release_marker


def build_snapshot(source: Path, destination: Path, source_sha: str) -> Path:
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_dir():
        raise FileNotFoundError(source)
    source_sha = source_sha.strip().lower()
    if len(source_sha) != 40 or any(ch not in "0123456789abcdef" for ch in source_sha):
        raise ValueError("source_sha must be a full 40-character Git revision")
    git_dir = source / ".git"
    if git_dir.exists():
        head = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip().lower()
        if head != source_sha:
            raise ValueError("source tree HEAD does not match source_sha")
        dirty = subprocess.run(
            ["git", "-C", str(source), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        if dirty:
            raise ValueError("source tree must be clean before snapshotting")
    if destination.exists():
        raise FileExistsError(destination)
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
    )
    stamp_release_marker(destination, source_sha)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a Hermes release snapshot")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("source_sha")
    args = parser.parse_args(argv)
    print(build_snapshot(args.source, args.destination, args.source_sha))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
