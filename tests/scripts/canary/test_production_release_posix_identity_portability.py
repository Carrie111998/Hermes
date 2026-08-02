from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from scripts.canary import production_release_builder_phase as phase
from scripts.canary import production_release_builder_runtime as runtime
from scripts.canary import production_release_candidate_promoter as promoter
from scripts.canary import production_release_rotation_stager_input_author as author
from scripts.canary import production_release_rotation_stager_installer as installer
from scripts.canary import production_release_rotation_stager_launcher as launcher
from scripts.canary import production_successor_rebind_owner_runtime as successor


@pytest.mark.parametrize(
    ("module", "error_type", "error_code"),
    (
        (
            phase,
            phase.ProductionReleaseBuilderPhaseError,
            "release_builder_phase_posix_identity_unavailable",
        ),
        (
            runtime,
            runtime.ProductionReleaseBuilderError,
            "production_release_builder_posix_identity_unavailable",
        ),
        (
            promoter,
            promoter.ProductionReleaseCandidatePromoterError,
            "candidate_promoter_posix_identity_unavailable",
        ),
        (
            author,
            author.RotationStagerInputAuthorError,
            "rotation_stager_input_posix_identity_unavailable",
        ),
        (
            installer,
            installer.RotationStagerInstallerError,
            "rotation_stager_installer_posix_identity_unavailable",
        ),
        (
            launcher,
            launcher.RotationStagerLauncherError,
            "rotation_stager_launcher_posix_identity_unavailable",
        ),
        (
            successor,
            successor.SuccessorRebindOwnerRuntimeError,
            "successor_rebind_owner_runtime_identity_unavailable",
        ),
    ),
)
def test_posix_identity_reader_fails_closed_when_api_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    error_type: type[RuntimeError],
    error_code: str,
) -> None:
    monkeypatch.setattr(module, "os", SimpleNamespace())

    for name in ("geteuid", "getegid"):
        with pytest.raises(error_type, match=rf"^{error_code}$"):
            module._read_posix_identity(name)


@pytest.mark.parametrize(
    "module",
    (phase, runtime, promoter, author, installer, launcher, successor),
)
def test_posix_identity_reader_preserves_native_values(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
) -> None:
    monkeypatch.setattr(
        module,
        "os",
        SimpleNamespace(
            geteuid=lambda: 12345,
            getegid=lambda: 23456,
        ),
    )

    assert module._read_posix_identity("geteuid") == 12345
    assert module._read_posix_identity("getegid") == 23456
