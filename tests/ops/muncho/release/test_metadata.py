from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from ops.muncho.release import cli
from hermes_cli import __version__
from ops.muncho.release.metadata import (
    REQUIRED_HISTORY_PREFIX,
    ReleaseMetadataError,
    canonical_bytes,
    load_optional_release_bundle,
    load_release_bundle,
    sha256_bytes,
    validate_release_history,
)


ROOT = Path(__file__).parents[4]


def _self_hash(value: dict, field: str) -> dict:
    unsigned = dict(value)
    unsigned.pop(field, None)
    return {**unsigned, field: sha256_bytes(canonical_bytes(unsigned))}


def test_bundled_metadata_keeps_hermes_version_separate_and_history_append_only():
    bundle = load_release_bundle(ROOT)

    assert bundle.metadata.version > bundle.history.releases[-1].version
    assert REQUIRED_HISTORY_PREFIX == (
        (
            "2.3.0",
            "62fbf327b3507a97a34807bf4834d35c396817de",
            "retrospective_baseline",
            False,
        ),
        (
            "2.3.1",
            "5564ec24a48d819e8ba0dd924bdb82ca5064ed4c",
            "retrospective_release",
            False,
        ),
    )
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == __version__
    assert str(bundle.metadata.version) != __version__


def test_upstream_tree_without_any_muncho_metadata_is_a_clean_optional_fallback(
    tmp_path: Path,
):
    assert load_optional_release_bundle(tmp_path) is None


def test_partial_or_invalid_metadata_fails_closed(tmp_path: Path):
    metadata = tmp_path / "ops/muncho/release/metadata.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("{}\n", encoding="ascii")

    with pytest.raises(
        ReleaseMetadataError,
        match="muncho_release_metadata_incomplete",
    ):
        load_optional_release_bundle(tmp_path)
    with pytest.raises(ReleaseMetadataError):
        load_release_bundle(tmp_path)


def test_release_cli_fails_closed_when_target_metadata_is_missing(
    tmp_path: Path,
    capsys,
):
    result = cli.main([
        "inspect",
        "--release-root",
        str(tmp_path),
        "--release-sha",
        "a" * 40,
    ])

    assert result == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "schema": "muncho-release-error.v1",
        "ok": False,
        "error": "muncho_release_metadata_missing",
    }


def test_history_rejects_one_version_for_two_shas_even_when_rehashed():
    bundle = load_release_bundle(ROOT)
    releases = [
        {
            "version": str(entry.version),
            "release_sha": entry.release_sha,
            "record_kind": entry.record_kind,
            "metadata_present_at_source": entry.metadata_present_at_source,
        }
        for entry in bundle.history.releases
    ]
    releases.append({
        "version": "2.3.1",
        "release_sha": "a" * 40,
        "record_kind": "source_release",
        "metadata_present_at_source": True,
    })
    value = _self_hash(
        {"schema": "muncho-release-history.v1", "releases": releases},
        "history_sha256",
    )

    with pytest.raises(
        ReleaseMetadataError,
        match="muncho_release_version_reused",
    ):
        validate_release_history(value)


def test_history_rejects_rewriting_the_retrospective_baseline():
    bundle = load_release_bundle(ROOT)
    releases = [
        {
            "version": str(entry.version),
            "release_sha": entry.release_sha,
            "record_kind": entry.record_kind,
            "metadata_present_at_source": entry.metadata_present_at_source,
        }
        for entry in bundle.history.releases
    ]
    releases[0]["release_sha"] = "f" * 40
    value = _self_hash(
        {"schema": "muncho-release-history.v1", "releases": releases},
        "history_sha256",
    )

    with pytest.raises(
        ReleaseMetadataError,
        match="muncho_release_history_baseline_changed",
    ):
        validate_release_history(value)
