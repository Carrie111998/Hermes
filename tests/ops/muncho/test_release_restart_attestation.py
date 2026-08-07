from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[3]
DEPLOY_HELPER = ROOT / "ops/muncho/runtime/muncho-auto-deploy-release"
RELEASE_SHA = "a" * 40


def _executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.parametrize(
    ("restart_attested", "expected_returncode", "announcement_expected"),
    ((False, 13, False), (True, 0, True)),
)
def test_already_active_wrapper_announces_only_with_durable_restart_proof(
    tmp_path: Path,
    restart_attested: bool,
    expected_returncode: int,
    announcement_expected: bool,
) -> None:
    releases = tmp_path / "releases"
    release = releases / f"hermes-agent-{RELEASE_SHA[:12]}"
    release.mkdir(parents=True)
    (release / ".codex-source-commit").write_text(RELEASE_SHA + "\n", encoding="ascii")
    active = tmp_path / "active"
    active.symlink_to(release, target_is_directory=True)
    operations = tmp_path / "operations.log"

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(
        fake_bin / "sudo",
        "#!/bin/sh\n"
        "printf '%s\\n' \"$TEST_RELEASE_SHA\"\n",
    )
    _executable(
        fake_bin / "systemctl",
        "#!/bin/sh\n"
        "if [ \"$1\" = is-active ]; then\n"
        "  if [ \"${2:-}\" = --quiet ]; then exit 0; fi\n"
        "  printf 'active\\n'; exit 0\n"
        "fi\n"
        "if [ \"$1\" = show ]; then printf '%s\\n' \"$TEST_INVOCATION_ID\"; exit 0; fi\n"
        "exit 97\n",
    )
    _executable(
        fake_bin / "readlink",
        f"#!{Path(sys.executable).resolve()}\n"
        "import os, sys\n"
        "print(os.path.realpath(sys.argv[-1]))\n",
    )

    command = r'''
source "$DEPLOY_HELPER"
RELEASES="$TEST_RELEASES"
ACTIVE_LINK="$TEST_ACTIVE"
SERVICE="hermes-cloud-gateway.service"
require_legacy_deploy_topology() { return 0; }
acquire_deploy_lock() { return 0; }
release_identity_matches() { return 0; }
attest_target_release_entrypoints() { return 0; }
attest_target_release_venv() { return 0; }
cutover_artifacts_match() { return 0; }
reserve_muncho_release_mapping() {
  printf 'reserve\n' >>"$TEST_OPERATIONS"
  return 0
}
complete_muncho_restart_attestation() {
  printf 'restart-complete:%s\n' "$3" >>"$TEST_OPERATIONS"
  [ "$TEST_RESTART_ATTESTED" = 1 ]
}
announce_muncho_release_after_smoke() {
  printf 'announce\n' >>"$TEST_OPERATIONS"
  printf '{"summary":"verified"}\n'
}
write_status() {
  printf 'status:%s:%s\n' "$1" "${4:-}" >>"$TEST_OPERATIONS"
}
run_deploy "$TEST_RELEASE_SHA" 364
'''
    completed = subprocess.run(
        ["bash", "-c", command],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DEPLOY_HELPER": str(DEPLOY_HELPER),
            "TEST_RELEASES": str(releases),
            "TEST_ACTIVE": str(active),
            "TEST_OPERATIONS": str(operations),
            "TEST_RELEASE_SHA": RELEASE_SHA,
            "TEST_INVOCATION_ID": "2" * 32,
            "TEST_RESTART_ATTESTED": "1" if restart_attested else "0",
        },
        timeout=20,
    )

    assert completed.returncode == expected_returncode, completed.stderr
    observed = operations.read_text(encoding="utf-8").splitlines()
    reserve_index = observed.index("reserve")
    restart_index = observed.index(f"restart-complete:{'2' * 32}")
    assert reserve_index < restart_index
    assert ("announce" in observed) is announcement_expected
    if restart_attested:
        assert any(line.startswith("status:deploy_pass:") for line in observed)
    else:
        assert any(
            "qualifying_restart_unattested" in line
            for line in observed
            if line.startswith(
                "status:deploy_smoke_passed_release_announcement_blocked:"
            )
        )
