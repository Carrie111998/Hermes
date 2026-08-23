"""Shared pre-mutation admission contract for Hermes updates.

Every mutating update surface calls :func:`perform_update` before it acquires
the update lock, snapshots state, invokes git/package tooling, or restarts a
runtime.  ``None`` authorizes the existing updater to continue;
``UpdateRefusal`` is a terminal, machine-readable refusal that has already
been persisted as a receipt.

The read-only :func:`evaluate_update_admission` variant supports update-check
surfaces.  Neither path uses a network request or subprocess, and the only
mutation performed here is the refusal receipt written by ``perform_update``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

IMAGE_MANAGED_UPDATE_REFUSED = "image_managed_update_refused"
UPDATE_REFUSED_EXIT = 2


@dataclass
class UpdateRefusal:
    """Stable refusal returned identically to CLI, API, and dashboard callers."""

    code: str
    message: str
    update_command: str
    deployment_kind: str
    install_method: str
    surface: str
    requested_target: Optional[str]
    classification_reason: str
    baked_identity: dict[str, Any]
    current_identity: dict[str, Any]
    correlation_id: Optional[str] = None
    receipt_path: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _image_refusal_message(plan: Any) -> str:
    provenance = plan.image_provenance or {}
    version = provenance.get("version") or plan.expected_version
    revision = provenance.get("revision")
    identity = ""
    if version:
        identity = f" (baked Hermes {version}"
        if revision:
            identity += f" @ {str(revision)[:12]}"
        identity += ")"
    elif revision:
        identity = f" (baked revision {str(revision)[:12]})"

    integrity_note = ""
    if not provenance.get("valid", True):
        integrity_note = (
            " The baked provenance marker is invalid; the runtime remains "
            "image-managed and is refusing closed."
        )

    return (
        f"{IMAGE_MANAGED_UPDATE_REFUSED}: This Hermes runtime is "
        f"image-managed{identity}; in-place update is "
        "refused before any mutation. Pull or select the desired image, then "
        "recreate the runtime through its deployment owner (Docker Compose, "
        "Kubernetes, Hermes Cloud, or equivalent)."
        f"{integrity_note}\n\nUpdate via: {plan.update_mechanism}"
    )


def evaluate_update_admission(
    *,
    surface: str,
    requested_target: Optional[str] = None,
    project_root: Optional[Path] = None,
    provenance_path: Optional[Path] = None,
    plan: Any = None,
) -> tuple[Any, Optional[UpdateRefusal]]:
    """Return ``(plan, refusal_or_none)`` using read-only probes only.

    The new typed refusal is intentionally gated on explicit image provenance,
    not the legacy ``.install_method=docker`` heuristic.  Images built before
    the marker therefore retain their existing refusal path while official
    marker-bearing images receive the Phase-3 contract.
    """

    if plan is None:
        # Marker absence is the compatibility boundary: ordinary Git,
        # package, Nix/APT, and legacy Docker installs must perform *zero*
        # Phase-3 inventory/config/identity work before their existing paths.
        # A positive marker is passed into inventory so the authoritative
        # observation cannot disappear across a second filesystem read.
        from hermes_cli.image_provenance import read_image_provenance

        # The reader itself converts every expected filesystem/validation
        # failure into either explicit absence or invalid-present provenance.
        # An unexpected programming error must stop admission, never be
        # reinterpreted as marker absence and authorize mutation.
        provenance = read_image_provenance(provenance_path)
        if provenance is None:
            return None, None

        from hermes_cli.update_inventory import collect_runtime_inventory

        plan = collect_runtime_inventory(
            project_root=project_root,
            provenance_path=provenance_path,
            _known_image_provenance=provenance,
            include_runtimes=False,
        )

    if plan.image_provenance is None:
        return plan, None

    provenance = plan.image_provenance
    refusal = UpdateRefusal(
        code=IMAGE_MANAGED_UPDATE_REFUSED,
        message=_image_refusal_message(plan),
        update_command=plan.update_mechanism,
        deployment_kind=plan.deployment_kind,
        install_method=plan.install_method,
        surface=surface,
        requested_target=requested_target,
        classification_reason=plan.classification_reason,
        baked_identity={
            "schema": provenance.get("schema"),
            "image": provenance.get("image"),
            "version": provenance.get("version"),
            "revision": provenance.get("revision"),
            "manager": provenance.get("manager"),
            "marker_path": provenance.get("marker_path"),
            "valid": provenance.get("valid", True),
            "error": provenance.get("error"),
        },
        current_identity={
            "sha": plan.expected_sha,
            "version": plan.expected_version,
        },
    )
    return plan, refusal


def _persist_refusal(plan: Any, refusal: UpdateRefusal) -> Optional[str]:
    """Attach the plan/refusal and finalize a durable receipt; never raises."""

    try:
        import hermes_cli.update_receipt as receipts
        from hermes_cli.update_inventory import record_plan_in_receipt

        # The baked marker is the sole identity authority for an official
        # image.  Thread that already-observed value through receipt creation
        # and finalization so neither step consults a bind-mounted checkout.
        code_identity = dict(refusal.current_identity)
        if not receipts.has_active_update_receipt():
            refusal.correlation_id = receipts.begin_update_receipt(
                surface=refusal.surface,
                requested_target=refusal.requested_target,
                code_identity=code_identity,
            )
        else:
            refusal.correlation_id = receipts.active_update_correlation_id()
            receipts.set_update_receipt_code_identity(code_identity)
        record_plan_in_receipt(plan)
        receipts.record_refusal(refusal.to_dict())
        path = receipts.finalize_update_receipt(
            "refused",
            stop_reason=refusal.code,
            code_identity=code_identity,
        )
        return str(path) if path is not None else None
    except Exception:
        return None


def perform_update(
    *,
    surface: str,
    requested_target: Optional[str] = None,
    project_root: Optional[Path] = None,
    provenance_path: Optional[Path] = None,
    plan: Any = None,
) -> Optional[UpdateRefusal]:
    """Run the shared mutating-update admission boundary.

    ``None`` allows the existing updater to proceed.  A returned refusal is
    final and durable; callers must surface it and stop with
    :data:`UPDATE_REFUSED_EXIT` before doing any other update work.
    """

    plan, refusal = evaluate_update_admission(
        surface=surface,
        requested_target=requested_target,
        project_root=project_root,
        provenance_path=provenance_path,
        plan=plan,
    )
    if refusal is None:
        return None
    refusal.receipt_path = _persist_refusal(plan, refusal)
    return refusal
