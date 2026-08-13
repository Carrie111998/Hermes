"""Deterministic quality control before an application is declared ready.

This is the last gate before a document reaches an employer, and the failure it
exists to stop is already in the historical record. Of 428 tailored packages on
this machine, **three carry a fabricated surname across both the resume and the
cover letter** — "Diego Rodrigues", "Diego Resende" — two of those with
placeholder emails (`diego@email.com`, `diego.rodrigues@email.com`). Another is
addressed to `[Company Name]`. Seven have a PDF older than the markdown it was
rendered from, so the file that actually gets sent is not the file that was
last edited.

None of that needs a model to find. A candidate's name and email are facts, and
a document contradicting them is wrong however well written it is. So the
deterministic pass runs first and the expensive semantic review never runs on a
package that fails it — the same ordering the rest of this workstream uses, for
the same reason.

Severity is not uniform. A missing file or an unfilled template marker is
`revise`: regenerate and it is gone. A fabricated identity is `blocked`,
because it is a failure of the generator's grip on ground truth and rerunning
the same generator may reproduce it. That one wants a person.

**Readiness is bound to bytes, not to a directory.** `check_application`
records a SHA-256 per artifact and :func:`is_ready` re-hashes before agreeing
that a verdict still applies. Without that, "QC passed" becomes a permanent
property of a folder whose contents have since changed — which is precisely how
an unreviewed document gets sent under a stale approval.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import hashlib
from pathlib import Path
import re
import unicodedata

# Rendered artifacts an application cannot be sent without.
REQUIRED_ARTIFACTS: tuple[str, ...] = (
    "resume.md",
    "resume.pdf",
    "cover-letter.md",
    "cover-letter.pdf",
)

# Sources paired with the rendering that is actually transmitted.
_RENDER_PAIRS: tuple[tuple[str, str], ...] = (
    ("resume.md", "resume.pdf"),
    ("cover-letter.md", "cover-letter.pdf"),
)

_TEXT_ARTIFACTS: tuple[str, ...] = ("resume.md", "cover-letter.md")

# Template markers left unfilled. Deliberately narrow: `[my portfolio](url)` is
# ordinary markdown and flagging it would train the operator to ignore this.
_PLACEHOLDER = re.compile(
    r"\[(?:company\s*name|insert[^\]]*|placeholder[^\]]*|your\s[^\]]*|todo[^\]]*|xxx+)\]"
    r"|\{\{[^}]{1,60}\}\}"
    r"|(?<![\w-])TBD(?![\w-])",
    re.IGNORECASE,
)

_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")

# The candidate's own identity block sits at the top. Scanning the whole
# document would flag every third party the letter legitimately names.
_IDENTITY_WINDOW_CHARS = 1200

# How far a rendered PDF may lag its markdown before it counts as stale.
#
# Measured across all 428 real packages, "md newer than pdf" is bimodal:
#   38.4s  39.5s  40.0s  55.0s  151.6s   <- one generation pass, write ordering
#   73414.0s (20.4h) x2                  <- genuinely regenerated afterwards
# Three orders of magnitude separate the groups, so any threshold inside that
# gap is safe. The original 1-second slack put 5 of 6 flagged packages in the
# wrong group — each blocking a submission because the markdown landed a minute
# after its PDF in the same run.
RENDER_SLACK_SECONDS = 900


class QCStatus(str, Enum):
    PASS = "pass"
    REVISE = "revise"
    BLOCKED = "blocked"


class QCFinding(str, Enum):
    MISSING_ARTIFACT = "missing_artifact"
    EMPTY_ARTIFACT = "empty_artifact"
    IDENTITY_MISMATCH = "identity_mismatch"
    UNFILLED_PLACEHOLDER = "unfilled_placeholder"
    STALE_RENDERING = "stale_rendering"
    PACKAGE_UNREADABLE = "package_unreadable"


# Findings travel in mailbox messages. A body excerpt would carry the resume
# with them, so each code maps to a fixed phrase and details name artifacts only.
_BLOCKING = frozenset({QCFinding.IDENTITY_MISMATCH, QCFinding.PACKAGE_UNREADABLE})


@dataclass(frozen=True)
class CandidateIdentity:
    """Ground truth for who the applicant is. Sourced from the master resume."""

    full_name: str
    email: str


@dataclass(frozen=True)
class Finding:
    code: QCFinding
    artifact: str
    detail: str


@dataclass(frozen=True)
class QCResult:
    status: QCStatus
    findings: tuple[Finding, ...]
    artifact_hashes: Mapping[str, str]
    policy_version: int


def _normalise(text: str) -> str:
    """Fold case and accents so `DIEGO DE ARAGAO` matches `Diego De Aragao`."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _identity_findings(
    artifact: str, text: str, identity: CandidateIdentity
) -> list[Finding]:
    findings: list[Finding] = []

    # The NAME is checked across the whole document, not the header. Measured
    # against 760 real artifacts: 654 name the candidate up top, but 98
    # legitimately name them only in the sign-off — a letter that opens with a
    # contact line and signs off underneath. Requiring the name in the header
    # blocked 27% of a known-good corpus. Absent *entirely* is the real defect,
    # and it caught 8 artifacts across 4 packages.
    if _normalise(identity.full_name) not in _normalise(text):
        findings.append(Finding(
            QCFinding.IDENTITY_MISMATCH, artifact,
            "candidate name appears nowhere in the artifact",
        ))

    # Any address other than the candidate's in their own header is either a
    # placeholder or another person's. Both are disqualifying.
    foreign = {
        e for e in _EMAIL.findall(text[:_IDENTITY_WINDOW_CHARS])
        if e.casefold() != identity.email.casefold()
    }
    if foreign:
        findings.append(Finding(
            QCFinding.IDENTITY_MISMATCH, artifact,
            "an email other than the candidate's appears in the identity block",
        ))
    return findings


def check_application(
    package_dir: Path,
    identity: CandidateIdentity,
    *,
    policy_version: int = 1,
) -> QCResult:
    """Judge one tailored application package without calling a model."""
    package_dir = Path(package_dir)
    if not package_dir.is_dir():
        return QCResult(
            status=QCStatus.BLOCKED,
            findings=(Finding(QCFinding.PACKAGE_UNREADABLE, str(package_dir.name),
                              "package directory does not exist"),),
            artifact_hashes={},
            policy_version=policy_version,
        )

    findings: list[Finding] = []
    hashes: dict[str, str] = {}

    for name in REQUIRED_ARTIFACTS:
        path = package_dir / name
        if not path.is_file():
            findings.append(Finding(QCFinding.MISSING_ARTIFACT, name,
                                    "required artifact is absent"))
            continue
        try:
            hashes[name] = _sha256(path)
            if path.stat().st_size == 0:
                findings.append(Finding(QCFinding.EMPTY_ARTIFACT, name,
                                        "artifact is empty"))
        except OSError:
            findings.append(Finding(QCFinding.MISSING_ARTIFACT, name,
                                    "artifact could not be read"))

    for name in _TEXT_ARTIFACTS:
        path = package_dir / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        findings.extend(_identity_findings(name, text, identity))
        if _PLACEHOLDER.search(text):
            findings.append(Finding(QCFinding.UNFILLED_PLACEHOLDER, name,
                                    "an unfilled template marker remains"))

    for source, rendered in _RENDER_PAIRS:
        src, dst = package_dir / source, package_dir / rendered
        if not (src.is_file() and dst.is_file()):
            continue
        # See RENDER_SLACK_SECONDS: the pair is written in one pass and the
        # markdown can land minutes after its PDF without anything being wrong.
        if src.stat().st_mtime > dst.stat().st_mtime + RENDER_SLACK_SECONDS:
            findings.append(Finding(QCFinding.STALE_RENDERING, rendered,
                                    "rendered file is older than its source"))

    if any(f.code in _BLOCKING for f in findings):
        status = QCStatus.BLOCKED
    elif findings:
        status = QCStatus.REVISE
    else:
        status = QCStatus.PASS

    return QCResult(
        status=status,
        findings=tuple(findings),
        artifact_hashes=dict(sorted(hashes.items())),
        policy_version=policy_version,
    )


def is_ready(result: QCResult, package_dir: Path) -> bool:
    """Does this verdict still authorize the files on disk right now?

    A pass applies to the bytes it read. If an artifact was edited, added or
    removed since, the verdict is void — re-run QC. Returning True here on
    changed content is how an unreviewed document reaches an employer under an
    old approval, so every uncertainty resolves to False.
    """
    if result.status is not QCStatus.PASS:
        return False

    package_dir = Path(package_dir)
    if not package_dir.is_dir():
        return False

    for name in REQUIRED_ARTIFACTS:
        path = package_dir / name
        recorded = result.artifact_hashes.get(name)
        if recorded is None or not path.is_file():
            return False
        try:
            if _sha256(path) != recorded:
                return False
        except OSError:
            return False
    return True


def load_identity(master_resume: Path) -> CandidateIdentity:
    """Read the candidate's ground truth from the master resume.

    The master resume is the only authoritative statement of who the applicant
    is; deriving identity from a generated artifact would let a fabrication
    validate itself.
    """
    text = Path(master_resume).read_text(encoding="utf-8", errors="replace")
    name_match = re.search(r"^#\s*(?:Master Resume\s*[—-]\s*)?([^,\n]+)", text, re.M)
    email_match = _EMAIL.search(text)
    if not name_match or not email_match:
        raise ValueError("master resume does not state a name and email")
    return CandidateIdentity(
        full_name=name_match.group(1).strip(),
        email=email_match.group(0).strip(),
    )
