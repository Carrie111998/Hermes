"""Shared CLI/gateway rendering for Hermes and optional Muncho identity."""

from __future__ import annotations

from pathlib import Path


def format_version_command_label(*, release_root: Path | None = None) -> str:
    """Return one honest version reply for every interactive surface.

    Official upstream installs have no Muncho metadata and retain the exact
    existing Hermes line.  A present but invalid fork metadata package is
    surfaced instead of silently presenting a potentially false Muncho alias.
    """

    from hermes_cli.banner import format_banner_version_label

    hermes_line = format_banner_version_label()
    try:
        from ops.muncho.release import (
            ReleaseMetadataError,
            load_optional_release_bundle,
            resolve_exact_release_sha,
        )

        try:
            bundle = load_optional_release_bundle(release_root)
        except ReleaseMetadataError:
            return f"Muncho release metadata: INVALID\n{hermes_line}"
        if bundle is None:
            return hermes_line
        release_sha = resolve_exact_release_sha(release_root)
    except ImportError:
        return hermes_line

    lines = [
        f"Muncho v{bundle.metadata.version}",
        hermes_line.replace("Hermes Agent", "Hermes upstream", 1),
    ]
    if release_sha is None:
        lines.append("Release SHA: unavailable")
    else:
        lines.append(f"Release SHA: {release_sha} (short {release_sha[:8]})")
    return "\n".join(lines)


__all__ = ["format_version_command_label"]
