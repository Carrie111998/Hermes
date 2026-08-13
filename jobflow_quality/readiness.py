"""The gate the applier consults immediately before submitting.

The ready sweep's ``material_files()`` checks only that files exist. That is how
four packages carrying a fabricated surname reached ``ready``, and how one
addressed to ``[Company Name]`` entered ``applying``. Existence is not fitness,
and by the time an application is out there is nothing to undo.

:func:`submission_block_reason` returns a bounded string or ``None``, shaped to
drop straight into the sweep's existing ``submitSkipped`` rows next to
``missing_materials_resume_pdf``.

**It fails closed.** Every path that cannot produce a clean PASS — a missing
directory, an unreadable package, a crash inside QC, an identity that could not
be loaded — returns a reason. Skipping costs one cron tick; submitting an
unchecked document costs an opportunity and cannot be recalled. If this ever
blocks everything because of a bug, the reason string says so explicitly rather
than looking like a quiet absence of work.
"""

from __future__ import annotations

from pathlib import Path

from .qc import CandidateIdentity, QCStatus, check_application, load_identity

DEFAULT_MASTER_RESUME = (
    Path.home() / ".hermes" / "profiles" / "cv-handler" / "workspace" / "kb"
    / "master-resume.md"
)


def load_default_identity(
    master_resume: Path | None = None,
) -> CandidateIdentity | None:
    """Read the candidate's ground truth, or None if it cannot be established.

    Returns None rather than a guess: an identity inferred from a generated
    artifact would let a fabrication validate itself, and the caller treats
    None as a reason to block.
    """
    path = Path(master_resume) if master_resume is not None else DEFAULT_MASTER_RESUME
    try:
        return load_identity(path)
    except (OSError, ValueError):
        return None


def submission_block_reason(
    materials_dir: Path,
    identity: CandidateIdentity | None,
    *,
    policy_version: int = 1,
) -> str | None:
    """Why this package must not be submitted, or None if it may be.

    Checked fresh at the moment of submission rather than trusting a stored
    verdict, because the artifacts may have been regenerated since anything was
    recorded about them.
    """
    if identity is None:
        return "qc_error:identity_unavailable"

    try:
        result = check_application(materials_dir, identity,
                                   policy_version=policy_version)
    except Exception as exc:
        # Never let a QC fault read as "nothing wrong". Only the exception
        # class travels — a message could carry document text.
        return f"qc_error:{type(exc).__name__}"

    if result.status is QCStatus.PASS:
        return None

    # Blocking findings first: a fabricated identity is the reason worth
    # showing when a package also has a fixable problem.
    codes = [f.code.value for f in result.findings]
    if result.status is QCStatus.BLOCKED:
        blocking = next(
            (c for c in codes if c in {"identity_mismatch", "package_unreadable"}),
            codes[0] if codes else "unknown",
        )
        return f"qc_blocked:{blocking}"
    return f"qc_revise:{codes[0] if codes else 'unknown'}"
