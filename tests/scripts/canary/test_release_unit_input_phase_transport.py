from __future__ import annotations

from typing import Any

import pytest

from scripts.canary import production_cutover_owner_launcher as owner
from scripts.canary import production_cutover_unit_input_rotation as rotation


OWNER_REVISION = "a" * 40
TARGET_REVISION = "b" * 40
TRANSACTION_SHA256 = "c" * 64
TRUST_SHA256 = "d" * 64
RECEIPT_SHA256 = "e" * 64
AUDIT_PATH = "/var/lib/muncho-production-legacy-cutover/exact-audit"


class _PrepareTransport:
    def __init__(self, tamper: str | None = None) -> None:
        self.request: dict[str, Any] | None = None
        self.tamper = tamper

    def invoke(
        self,
        revision: str,
        action: str,
        *,
        release_rotation_request: dict[str, Any],
    ) -> dict[str, Any]:
        assert revision == TARGET_REVISION
        assert action == "prepare-release-unit-inputs"
        self.request = release_rotation_request
        receipt = {
            "transaction_sha256": TRANSACTION_SHA256,
            "audit_transaction_path": AUDIT_PATH,
            "receipt_sha256": RECEIPT_SHA256,
        }
        unsigned: dict[str, Any] = {
            "schema": rotation.RELEASE_PHASE_RESULT_SCHEMA,
            "action": action,
            "owner_release_revision": OWNER_REVISION,
            "remote_stager_revision": TARGET_REVISION,
            "request_sha256": release_rotation_request["request_sha256"],
            "transaction_sha256": TRANSACTION_SHA256,
            "audit_transaction_path": AUDIT_PATH,
            "canonical_receipt": receipt,
            "canonical_receipt_sha256": RECEIPT_SHA256,
            "activation_begin": None,
            "secret_material_recorded": False,
            "secret_digest_recorded": False,
        }
        if self.tamper == "transaction":
            unsigned["transaction_sha256"] = "f" * 64
        elif self.tamper == "audit_path":
            unsigned["audit_transaction_path"] = "/var/lib/other"
        elif self.tamper == "activation":
            unsigned["activation_begin"] = "not-a-mapping"
        elif self.tamper == "request":
            unsigned["request_sha256"] = "f" * 64
        elif self.tamper == "artifact_digest":
            unsigned["canonical_receipt_sha256"] = "f" * 64
        return {
            **unsigned,
            "result_sha256": owner._sha(owner._canonical(unsigned)),
        }


def _run_prepare(
    monkeypatch: pytest.MonkeyPatch,
    transport: _PrepareTransport,
) -> dict[str, Any]:
    monkeypatch.setattr(
        rotation,
        "validate_release_prepared_rotation_receipt",
        lambda value, **_kwargs: dict(value),
    )
    return dict(
        owner.run_release_unit_input_phase(
            action="prepare-release-unit-inputs",
            owner_release_revision=OWNER_REVISION,
            remote_stager_revision=TARGET_REVISION,
            unit_input_publication={
                "release_revision": TARGET_REVISION,
            },
            release_update_publication={
                "release_revision": TARGET_REVISION,
            },
            trusted_predecessor={"trust_sha256": TRUST_SHA256},
            expected_predecessor_trust_sha256=TRUST_SHA256,
            transport=transport,
        )
    )


def test_owner_release_phase_binds_revisions_and_exact_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _PrepareTransport()

    result = _run_prepare(monkeypatch, transport)

    assert result["transaction_sha256"] == TRANSACTION_SHA256
    assert transport.request is not None
    request = transport.request
    assert request["owner_release_revision"] == OWNER_REVISION
    assert request["remote_stager_revision"] == TARGET_REVISION
    unsigned = {
        key: value
        for key, value in request.items()
        if key != "request_sha256"
    }
    assert request["request_sha256"] == owner._sha(
        owner._canonical(unsigned)
    )


@pytest.mark.parametrize(
    "tamper",
    (
        "transaction",
        "audit_path",
        "activation",
        "request",
        "artifact_digest",
    ),
)
def test_owner_release_phase_rejects_rehashed_wrapper_tamper(
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    with pytest.raises(
        owner.OwnerCutoverError,
        match="owner_cutover_release_unit_input_phase_invalid",
    ):
        _run_prepare(monkeypatch, _PrepareTransport(tamper))


def test_owner_release_phase_rejects_target_publication_mismatch_before_iap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _PrepareTransport()
    monkeypatch.setattr(
        rotation,
        "validate_release_prepared_rotation_receipt",
        lambda value, **_kwargs: dict(value),
    )

    with pytest.raises(
        owner.OwnerCutoverError,
        match="owner_cutover_release_unit_input_phase_invalid",
    ):
        owner.run_release_unit_input_phase(
            action="prepare-release-unit-inputs",
            owner_release_revision=OWNER_REVISION,
            remote_stager_revision=TARGET_REVISION,
            unit_input_publication={
                "release_revision": "f" * 40,
            },
            release_update_publication={
                "release_revision": TARGET_REVISION,
            },
            trusted_predecessor={"trust_sha256": TRUST_SHA256},
            expected_predecessor_trust_sha256=TRUST_SHA256,
            transport=transport,
        )

    assert transport.request is None
