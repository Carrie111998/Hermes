"""Gate audit — which cron jobs have a check that can actually fail.

Playbook Gap 2 (``docs/agents-loops-graphs.md`` §3): a scheduled job's output is
only trustworthy when something *other than the model that produced it* decides
whether it passed. The ``[SILENT]`` sentinel does not qualify — it is
self-reported. Two gate shapes exist in this codebase today:

  * ``no_agent`` + ``script`` — the script's exit code IS the gate. Mechanical,
    free, and it cannot hallucinate a pass.
  * a **chained checker** — a second job whose ``context_from`` names the
    producer, running a *different* model, whose only task is to grade the
    producer's output against criteria.

Auditing this by hand does not scale and does not stay done, so the sweep is
mechanical: :func:`audit_jobs` takes job dicts (as stored by ``cron/jobs.py``)
and returns findings. Every rule is decidable from the job records alone — no
model judges anything here, which is the same principle the audit is enforcing.

Pure functions: no I/O, no scheduler, no network. The caller supplies the jobs
and decides what to do with the findings (report, suggest, or ignore).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

# Prompt markers for work whose answer is a fact the machine already knows:
# a count, a status, a diff, a reachability check. These are the jobs the
# playbook says to demote to `no_agent` scripts — an LLM adds cost and a
# hallucination surface to a question `curl`/`systemctl`/`df` already answers.
_MECHANICAL_MARKERS: Sequence[str] = (
    r"\bcheck (?:if|whether|that)\b",
    r"\bis .{0,40}\b(?:up|down|running|reachable|alive|healthy)\b",
    r"\bdisk (?:space|usage)\b",
    r"\bfree space\b",
    r"\bping\b",
    r"\bhttp status\b",
    r"\bexit code\b",
    r"\bcount (?:the )?(?:number of|open|failed|stale)\b",
    r"\bhow many\b",
    r"\bservice status\b",
    r"\bsystemctl\b",
    r"\bcertificate expir",
    r"\buptime\b",
)

# Words that mark an output a human or downstream job acts on. An ungated job
# whose output only informs is a nuisance; an ungated job that feeds a decision
# is a correctness risk, so the two are ranked differently.
_DECISION_MARKERS: Sequence[str] = (
    r"\brecommend",
    r"\bdecide\b",
    r"\bapprove\b",
    r"\bescalate\b",
    r"\bprioriti[sz]e\b",
    r"\bwhat should\b",
    r"\baction items?\b",
    r"\bnext steps?\b",
    r"\btriage\b",
)

_MECHANICAL_RE = re.compile("|".join(_MECHANICAL_MARKERS), re.IGNORECASE)
_DECISION_RE = re.compile("|".join(_DECISION_MARKERS), re.IGNORECASE)

# Finding kinds. Stable strings — callers and tests match on these.
UNGATED_AGENT_JOB = "ungated_agent_job"
MECHANICAL_LLM_JOB = "mechanical_llm_job"
CHECKER_SHARES_MODEL = "checker_shares_model"
SELF_REFERENTIAL_CONTEXT = "self_referential_context"

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True)
class Finding:
    """One audited job and what is wrong with its gate."""

    kind: str
    job_id: str
    job_name: str
    severity: str
    summary: str
    recommendation: str
    # Other job IDs the finding implicates (e.g. the producer a checker grades).
    related: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "job_id": self.job_id,
            "job_name": self.job_name,
            "severity": self.severity,
            "summary": self.summary,
            "recommendation": self.recommendation,
            "related": list(self.related),
        }


def _job_id(job: Dict[str, Any]) -> str:
    return str(job.get("id") or job.get("job_id") or "")


def _job_name(job: Dict[str, Any]) -> str:
    return str(job.get("name") or job.get("id") or "<unnamed>")


def _prompt_text(job: Dict[str, Any]) -> str:
    """Everything a rule should read as 'what this job was told to do'."""
    parts = [job.get("prompt"), job.get("description"), job.get("name")]
    return " ".join(str(p) for p in parts if p)


def _context_from(job: Dict[str, Any]) -> List[str]:
    """``context_from`` is stored as a list, but accept a bare string too."""
    raw = job.get("context_from")
    if not raw:
        return []
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    return [str(item).strip() for item in raw if str(item).strip()]


def _model_of(job: Dict[str, Any]) -> Optional[str]:
    model = job.get("model") or job.get("model_name")
    return str(model).strip() or None if model else None


def _is_agent_job(job: Dict[str, Any]) -> bool:
    """True when a model produces the output (i.e. not a script-only job)."""
    return not bool(job.get("no_agent"))


def _is_enabled(job: Dict[str, Any]) -> bool:
    """Paused and disabled jobs are not worth reporting on."""
    if job.get("enabled") is False:
        return False
    status = str(job.get("status") or "").lower()
    return status not in {"paused", "disabled", "archived"}


def looks_mechanical(job: Dict[str, Any]) -> bool:
    """True when the job asks a question a script could answer outright."""
    return bool(_MECHANICAL_RE.search(_prompt_text(job)))


def feeds_a_decision(job: Dict[str, Any]) -> bool:
    """True when the output is meant to be acted on, not merely read."""
    return bool(_DECISION_RE.search(_prompt_text(job)))


def audit_jobs(jobs: Iterable[Dict[str, Any]]) -> List[Finding]:
    """Return every gate finding across ``jobs``, worst first.

    Rules, in the order the playbook prioritizes them:

    1. An agent job that nothing checks is ungated — nothing but the model
       decides it succeeded.
    2. An agent job asking a mechanical question should be a ``no_agent``
       script, whose exit code is a free and honest gate.
    3. A checker pinned to the same model as its producer is not an
       independent check; "worker and checker never share a context" is the
       whole point of chaining one.
    4. A job whose ``context_from`` names itself grades its own homework.

    Ordering is stable: severity first, then the order the jobs came in, so
    repeated runs over an unchanged store produce an identical report.
    """
    job_list = [j for j in jobs if isinstance(j, dict)]
    active = [j for j in job_list if _is_enabled(j)]
    by_id = {_job_id(j): j for j in job_list if _job_id(j)}
    order = {_job_id(j): i for i, j in enumerate(job_list) if _job_id(j)}

    # Who checks whom. A job with `context_from: [X]` is treated as a checker
    # for X — the chained-checker pattern is the only thing that edge is for.
    checked_by: Dict[str, List[str]] = {}
    for job in active:
        jid = _job_id(job)
        for producer in _context_from(job):
            if producer and producer != jid:
                checked_by.setdefault(producer, []).append(jid)

    findings: List[Finding] = []

    for job in active:
        jid = _job_id(job)
        name = _job_name(job)

        if jid and jid in _context_from(job):
            findings.append(
                Finding(
                    kind=SELF_REFERENTIAL_CONTEXT,
                    job_id=jid,
                    job_name=name,
                    severity="high",
                    summary=(
                        f"{name} lists itself in context_from — it is grading "
                        "its own previous output."
                    ),
                    recommendation=(
                        "Drop the self-reference. If this job needs a gate, add "
                        "a separate checker job whose context_from names it and "
                        "which runs a different model."
                    ),
                )
            )

        if not _is_agent_job(job):
            # Script-only: the exit code is the gate. Nothing to report.
            continue

        if looks_mechanical(job):
            findings.append(
                Finding(
                    kind=MECHANICAL_LLM_JOB,
                    job_id=jid,
                    job_name=name,
                    severity="medium",
                    summary=(
                        f"{name} asks a mechanical question but runs a model to "
                        "answer it."
                    ),
                    recommendation=(
                        "Convert to no_agent=True with a script that exits "
                        "non-zero on failure. Reserve the model for jobs that "
                        "need reasoning, fed by --script output."
                    ),
                )
            )

        checkers = checked_by.get(jid, [])
        if not checkers:
            decision = feeds_a_decision(job)
            findings.append(
                Finding(
                    kind=UNGATED_AGENT_JOB,
                    job_id=jid,
                    job_name=name,
                    severity="high" if decision else "medium",
                    summary=(
                        f"{name} has no gate — nothing but the model decides "
                        "whether its output is good."
                        + (" Its output feeds a decision." if decision else "")
                    ),
                    recommendation=(
                        "Add a checker job with context_from=['" + jid + "'] on "
                        "a different (cheap) model that replies PASS or a "
                        "one-line failure — or convert this job to a no_agent "
                        "script if the check is mechanical."
                    ),
                )
            )
            continue

        producer_model = _model_of(job)
        for checker_id in checkers:
            checker = by_id.get(checker_id)
            if checker is None or not _is_agent_job(checker):
                # A script checker is independent by construction.
                continue
            checker_model = _model_of(checker)
            if producer_model and checker_model and producer_model != checker_model:
                continue
            findings.append(
                Finding(
                    kind=CHECKER_SHARES_MODEL,
                    job_id=checker_id,
                    job_name=_job_name(checker),
                    severity="medium",
                    summary=(
                        f"{_job_name(checker)} checks {name} but is not pinned "
                        "to a different model."
                    ),
                    recommendation=(
                        "Pin the checker to a different, cheaper model than the "
                        "producer. A checker sharing the producer's model shares "
                        "its blind spots."
                    ),
                    related=[jid],
                )
            )

    findings.sort(
        key=lambda f: (
            _SEVERITY_ORDER.get(f.severity, 99),
            order.get(f.job_id, 10**6),
            f.kind,
        )
    )
    return findings


def format_audit_report(findings: Sequence[Finding]) -> str:
    """Render findings as plain text for a CLI or a cron delivery."""
    if not findings:
        return "cron gate audit: every active job has a check that can fail ✅"

    lines = [f"cron gate audit: {len(findings)} finding(s)", ""]
    current = None
    for finding in findings:
        if finding.severity != current:
            current = finding.severity
            lines.append(f"  {current.upper()}")
        lines.append(f"    • {finding.summary}")
        lines.append(f"      → {finding.recommendation}")
    return "\n".join(lines)
