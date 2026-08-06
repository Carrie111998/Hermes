"""Regression test: ``hermes dump`` reports a real git SHA inside the container.

Background: ``.dockerignore`` excludes ``.git``, so ``git rev-parse HEAD``
fails inside the published image and ``hermes dump`` used to report
``version: ... [(unknown)]``.  The Dockerfile now writes the build-time
``$HERMES_GIT_SHA`` build-arg to ``/opt/hermes/.hermes_build_sha`` and
``hermes_cli/build_info.py`` reads it as a fallback.

CI (``.github/workflows/docker.yml``) always sets the build-arg
to ``${{ github.sha }}``.  Local ``docker build`` (the ``built_image``
fixture in ``tests/docker/conftest.py``) does NOT — so locally the file
is absent and ``hermes dump`` correctly falls back to ``(unknown)``.

This test handles both cases:

* If ``/opt/hermes/.hermes_build_sha`` exists in the image, assert that
  ``hermes dump`` surfaces its content as the version SHA (not
  ``(unknown)``).
* If the file is absent, assert the legacy behaviour (``(unknown)``)
  still holds — defensive guard against the helper accidentally
  reporting bogus data from somewhere else.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

_VERSION_LINE = re.compile(r"^version:\s+(?P<rest>.+)$", re.MULTILINE)
_SHA_BRACKET = re.compile(r"\[(?P<sha>[^\]]+)\]\s*$")


def _run_dump(image: str) -> str:
    """Return the stdout of ``docker run <image> dump``.

    Relies on Docker's anonymous VOLUME for ``/opt/data`` (declared by the
    Dockerfile) so the container's hermes user (UID 10000) can bootstrap
    its config.  Anonymous volumes are auto-cleaned by ``--rm``, so unlike
    a host bind-mount we don't have to chown anything to UID 10000 (which
    would break cleanup on non-root hosts).
    """
    r = subprocess.run(
        ["docker", "run", "--rm", image, "dump"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, (
        f"hermes dump exited {r.returncode}: "
        f"stderr={r.stderr[-1000:]!r}\nstdout={r.stdout[-1000:]!r}"
    )
    return r.stdout


def _read_baked_sha_from_image(image: str) -> str | None:
    """Return the ``/opt/hermes/.hermes_build_sha`` content, or None if absent."""
    r = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "cat",
            image,
            "/opt/hermes/.hermes_build_sha",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _read_baked_metadata_from_image(image: str) -> dict[str, object] | None:
    """Return the release contract from the image, if this is a release build."""
    r = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "cat",
            image,
            "/opt/hermes/.hermes_build_metadata.json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if r.returncode != 0:
        return None
    payload = json.loads(r.stdout)
    assert isinstance(payload, dict)
    return payload


def test_dockerfile_stamps_provenance_in_code_tree_without_runtime_env() -> None:
    """Docker delegates the contract to the one Runtime implementation."""
    dockerfile = Path(__file__).parents[2] / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")
    start = text.index("# ---------- Bake build-time Runtime provenance ----------")
    end = text.index("# ---------- s6-overlay service wiring ----------", start)
    provenance_stanza = text[start:end]

    assert "ARG HERMES_GIT_SHA=" in provenance_stanza
    assert "ARG HERMES_BUILD_TIMESTAMP=" in provenance_stanza
    assert "ARG RELEASE_ID=" in provenance_stanza
    assert "python3 gateway/runtime_provenance.py stamp-build" in provenance_stanza
    assert '"${HERMES_GIT_SHA}" "${RELEASE_ID}"' in provenance_stanza
    assert '"${HERMES_BUILD_TIMESTAMP}"' in provenance_stanza
    assert "RUN <<" not in provenance_stanza
    assert "python3 -" not in provenance_stanza
    assert not re.search(
        r"^\s*ENV\s+HERMES_(?:GIT_SHA|BUILD_TIMESTAMP|RELEASE_ID)=",
        text,
        re.MULTILINE,
    )


def test_image_metadata_is_allowlisted_and_legacy_sha_remains_compatible(
    built_image: str,
) -> None:
    """Release images expose only the contract; local images stay incomplete."""
    metadata = _read_baked_metadata_from_image(built_image)
    baked_sha = _read_baked_sha_from_image(built_image)

    if metadata is None:
        # The local fixture has no provenance args.  A legacy SHA-only image
        # is also allowed until its release build supplies the full contract.
        assert baked_sha is None or re.fullmatch(r"[0-9a-f]{40,64}", baked_sha)
        return

    assert set(metadata) == {
        "format_version",
        "git_commit",
        "release_id",
        "build_timestamp",
        "runtime_sha256",
    }
    assert metadata["format_version"] == 1
    assert isinstance(metadata["git_commit"], str)
    assert isinstance(metadata["release_id"], str)
    assert isinstance(metadata["build_timestamp"], str)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", str(metadata["runtime_sha256"]))
    assert baked_sha == metadata["git_commit"]


def test_dump_reports_baked_sha_when_present(built_image: str) -> None:
    """When the image was built with ``HERMES_GIT_SHA``, dump must surface it.

    Together with the smoke-test action (which exercises ``--help``), this
    closes the regression loop for the missing-sha bug: any future change
    that breaks the baked-file -> dump pipeline will fail CI here.
    """
    baked = _read_baked_sha_from_image(built_image)
    stdout = _run_dump(built_image)

    match = _VERSION_LINE.search(stdout)
    assert match, f"no `version:` line in dump output:\n{stdout[:2000]}"
    sha_match = _SHA_BRACKET.search(match.group("rest"))
    assert sha_match, (
        f"`version:` line missing [<sha>] bracket: {match.group('rest')!r}"
    )
    reported = sha_match.group("sha")

    if baked is None:
        # Local-build path: no build-arg was passed.  Verify the legacy
        # fallback ``(unknown)`` is intact — guards against the helper
        # ever inventing a SHA from thin air.
        assert reported == "(unknown)", (
            f"expected '(unknown)' when no SHA baked, got {reported!r}"
        )
        return

    # CI path: build-arg was set, baked file exists.  ``hermes dump``
    # truncates to 8 chars via ``git rev-parse --short=8`` semantics.
    assert reported != "(unknown)", (
        "baked SHA file present in image but dump still reported "
        f"'(unknown)' — the build-info fallback is broken.  "
        f"Baked file content: {baked!r}"
    )
    assert reported == baked[:8], (
        f"dump reported {reported!r} but baked file contained {baked!r} "
        f"(expected first 8 chars: {baked[:8]!r})"
    )
