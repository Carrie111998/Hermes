import json
from pathlib import Path

import pytest

from gateway import code_skew
from hermes_cli.gateway_identity import (
    GatewayIdentityError,
    identity_from_project,
    parse_systemd_properties,
)


def test_release_identity_requires_one_consistent_marker(tmp_path: Path) -> None:
    marker = tmp_path / ".hermes-release-sha"
    marker.write_text("a" * 40 + "\n", encoding="utf-8")

    identity = identity_from_project(tmp_path, allow_git_fallback=False)

    assert identity.fingerprint == "release:" + ("a" * 40)
    assert identity.source == "release"


def test_release_identity_rejects_conflicting_markers(tmp_path: Path) -> None:
    (tmp_path / ".hermes-release-sha").write_text("a" * 40, encoding="utf-8")
    (tmp_path / "RELEASE_SHA").write_text("b" * 40, encoding="utf-8")

    with pytest.raises(GatewayIdentityError, match="conflicting release markers"):
        identity_from_project(tmp_path, allow_git_fallback=False)


def test_parse_systemd_properties_validates_required_identity_fields() -> None:
    props = parse_systemd_properties(
        "ActiveState=active\n"
        "SubState=running\n"
        "MainPID=123\n"
        "WorkingDirectory=/srv/hermes/releases/abc\n"
    )

    assert props["MainPID"] == "123"


def test_read_boot_record_accepts_json_and_legacy_values(tmp_path: Path) -> None:
    record_path = tmp_path / "gateway_boot_fingerprint"
    record_path.write_text(json.dumps({"fingerprint": "release:" + ("c" * 40)}))
    assert code_skew.read_boot_record(tmp_path)["fingerprint"] == "release:" + ("c" * 40)

    record_path.write_text("git:refs/heads/main:def456\n", encoding="utf-8")
    record = code_skew.read_boot_record(tmp_path)
    assert record == {
        "schema": 0,
        "fingerprint": "git:refs/heads/main:def456",
        "release_path": None,
    }