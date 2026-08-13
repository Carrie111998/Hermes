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

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from .qc import CandidateIdentity, QCResult, QCStatus, check_application, load_identity
from .semantic_qc import SemanticStatus, review

DEFAULT_MASTER_RESUME = (
    Path.home() / ".hermes" / "profiles" / "cv-handler" / "workspace" / "kb"
    / "master-resume.md"
)

DEFAULT_SEMANTIC_CACHE = (
    Path.home() / ".hermes" / "profiles" / "applier" / "workspace"
    / "semantic-qc-cache.json"
)


def _artifact_fingerprint(result: QCResult) -> str:
    """One key for one exact set of artifact bytes."""
    payload = json.dumps(dict(sorted(result.artifact_hashes.items())),
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _read_cache(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        # A corrupt or absent cache means "not reviewed yet", never a verdict.
        return {}


def _write_cache(path: Path, data: dict[str, Any]) -> None:
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        # The cache is an optimisation. Losing it costs a repeat review, not a
        # wrong verdict, so a write failure is never fatal.
        pass


def semantic_verdict(
    materials_dir: Path,
    identity: CandidateIdentity,
    *,
    invoke: Callable[[str], Any],
    cache_path: Path | None = None,
    master_resume: Path | None = None,
    job_description: str = "",
):
    """Review a package, reusing a cached verdict for unchanged artifacts.

    The ready sweep runs every three hours; without this cache each eligible
    package would buy ~8 premium reviews a day to reach the same answer. The
    key is the artifact fingerprint, so regenerating a resume earns a fresh
    review and leaving it alone does not.

    UNKNOWN is never cached — caching a provider outage would make it permanent
    for those exact bytes.
    """
    path = Path(cache_path) if cache_path is not None else DEFAULT_SEMANTIC_CACHE
    result = check_application(materials_dir, identity)
    key = _artifact_fingerprint(result)

    cache = _read_cache(path)
    hit = cache.get(key)
    if isinstance(hit, dict) and hit.get("status") in (
        SemanticStatus.PASS.value, SemanticStatus.FINDINGS.value
    ):
        from .semantic_qc import SemanticFinding, SemanticResult

        return SemanticResult(
            SemanticStatus(hit["status"]),
            tuple(SemanticFinding(**f) for f in hit.get("findings", [])),
        )

    master_path = Path(master_resume) if master_resume is not None else DEFAULT_MASTER_RESUME
    try:
        master_text = master_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        master_text = ""

    def _read(name: str) -> str:
        try:
            return (Path(materials_dir) / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    verdict = review(
        master_resume=master_text,
        job_description=job_description,
        resume=_read("resume.md"),
        cover_letter=_read("cover-letter.md"),
        invoke=invoke,
    )

    if verdict.status is not SemanticStatus.UNKNOWN:
        cache[key] = {
            "status": verdict.status.value,
            "findings": [vars(f) for f in verdict.findings],
        }
        _write_cache(path, cache)
    return verdict


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
    invoke: Callable[[str], Any] | None = None,
    cache_path: Path | None = None,
    job_description: str = "",
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
        if invoke is None:
            # No reviewer configured. The deterministic gate has passed and
            # blocking every submission because an optional pass is unwired
            # would be a worse failure than not running it.
            return None
        try:
            verdict = semantic_verdict(
                materials_dir, identity, invoke=invoke,
                cache_path=cache_path, job_description=job_description,
            )
        except Exception as exc:
            return f"qc_error:semantic_{type(exc).__name__}"
        if verdict.status is SemanticStatus.PASS:
            return None
        if verdict.status is SemanticStatus.UNKNOWN:
            # Fail closed: an unreviewed document is not a reviewed one.
            return "qc_semantic:unknown"
        codes = sorted({f.category for f in verdict.findings})
        return f"qc_semantic:{codes[0] if codes else 'findings'}"

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
