"""Tests for cron/gate_audit.py — the Gap 2 sweep.

Every rule must be decidable from job records alone, so these tests drive the
auditor with plain dicts shaped like the ones cron/jobs.py stores. The point of
the audit is that a job's gate is real, so the tests are written around the two
gate shapes the playbook names: a no_agent script's exit code, and a chained
checker on a different model.
"""

from cron.gate_audit import (
    CHECKER_SHARES_MODEL,
    MECHANICAL_LLM_JOB,
    SELF_REFERENTIAL_CONTEXT,
    UNGATED_AGENT_JOB,
    audit_jobs,
    feeds_a_decision,
    format_audit_report,
    looks_mechanical,
    suggest_audit_remediations,
)


def job(**kwargs):
    base = {"id": "j1", "name": "job one", "prompt": "do the thing"}
    base.update(kwargs)
    return base


def kinds(findings):
    return {f.kind for f in findings}


def by_kind(findings, kind):
    return [f for f in findings if f.kind == kind]


# ── the two real gates produce no findings ───────────────────────────────────

def test_no_agent_script_job_is_gated_by_its_exit_code():
    findings = audit_jobs([job(no_agent=True, script="check_disk.sh")])
    assert findings == []


def test_chained_checker_on_a_different_model_is_a_real_gate():
    jobs = [
        job(id="prod", name="weekly digest", model="opus"),
        job(
            id="check",
            name="digest checker",
            model="haiku",
            context_from=["prod"],
            prompt="reply PASS or a one-line failure",
        ),
    ]
    findings = audit_jobs(jobs)
    # The producer is gated. The checker itself is ungated, which is correct
    # and expected — but it must not be reported as sharing the producer's model.
    assert CHECKER_SHARES_MODEL not in kinds(findings)
    assert "prod" not in {f.job_id for f in by_kind(findings, UNGATED_AGENT_JOB)}


# ── rule 1: an agent job nothing checks is ungated ───────────────────────────

def test_agent_job_with_no_checker_is_ungated():
    findings = by_kind(audit_jobs([job()]), UNGATED_AGENT_JOB)
    assert len(findings) == 1
    assert findings[0].job_id == "j1"


def test_decision_feeding_job_outranks_an_informational_one():
    jobs = [
        job(id="info", name="fyi", prompt="summarize yesterday's commits"),
        job(id="act", name="triage", prompt="recommend next steps for the team"),
    ]
    findings = {f.job_id: f for f in by_kind(audit_jobs(jobs), UNGATED_AGENT_JOB)}
    assert findings["act"].severity == "high"
    assert findings["info"].severity == "medium"
    # Severity drives ordering, so the decision-feeding job is reported first.
    assert audit_jobs(jobs)[0].job_id == "act"


def test_a_script_checker_counts_as_a_gate():
    # Independence is structural for a script — it cannot share a context.
    jobs = [
        job(id="prod", name="producer"),
        job(id="check", no_agent=True, script="grade.sh", context_from=["prod"]),
    ]
    ungated = {f.job_id for f in by_kind(audit_jobs(jobs), UNGATED_AGENT_JOB)}
    assert "prod" not in ungated


# ── rule 2: mechanical questions belong in scripts ───────────────────────────

def test_mechanical_prompt_is_flagged_for_demotion_to_a_script():
    j = job(prompt="Check whether the gateway is reachable and report status")
    assert looks_mechanical(j)
    assert MECHANICAL_LLM_JOB in kinds(audit_jobs([j]))


def test_reasoning_prompt_is_not_flagged_as_mechanical():
    j = job(prompt="Draft a short narrative summary of this week's incidents")
    assert not looks_mechanical(j)
    assert MECHANICAL_LLM_JOB not in kinds(audit_jobs([j]))


def test_mechanical_no_agent_job_is_left_alone():
    # Already a script — this is the fixed state, not a finding.
    j = job(prompt="check if redis is up", no_agent=True, script="redis_up.sh")
    assert audit_jobs([j]) == []


# ── rule 3: a checker must not share the producer's model ────────────────────

def test_checker_pinned_to_the_same_model_is_flagged():
    jobs = [
        job(id="prod", name="producer", model="opus"),
        job(id="check", name="checker", model="opus", context_from=["prod"]),
    ]
    findings = by_kind(audit_jobs(jobs), CHECKER_SHARES_MODEL)
    assert len(findings) == 1
    assert findings[0].job_id == "check"
    assert findings[0].related == ["prod"]


def test_checker_with_no_model_pinned_is_flagged():
    # Unpinned means both resolve to the same default — same blind spots.
    jobs = [
        job(id="prod", name="producer"),
        job(id="check", name="checker", context_from=["prod"]),
    ]
    assert CHECKER_SHARES_MODEL in kinds(audit_jobs(jobs))


# ── rule 4: no grading your own homework ─────────────────────────────────────

def test_self_referential_context_from_is_flagged_high():
    findings = by_kind(
        audit_jobs([job(id="j1", context_from=["j1"])]), SELF_REFERENTIAL_CONTEXT
    )
    assert len(findings) == 1
    assert findings[0].severity == "high"


def test_self_reference_does_not_count_as_being_checked():
    # The bug this guards: a self-edge populating checked_by would make the
    # job look gated, silently suppressing the ungated finding.
    findings = audit_jobs([job(id="j1", context_from=["j1"])])
    assert UNGATED_AGENT_JOB in kinds(findings)


# ── scope and hygiene ────────────────────────────────────────────────────────

def test_paused_and_disabled_jobs_are_not_audited():
    jobs = [
        job(id="a", status="paused"),
        job(id="b", enabled=False),
        job(id="c", status="archived"),
    ]
    assert audit_jobs(jobs) == []


def test_context_from_accepts_a_bare_string():
    jobs = [
        job(id="prod", name="producer", model="opus"),
        job(id="check", name="checker", model="haiku", context_from="prod"),
    ]
    ungated = {f.job_id for f in by_kind(audit_jobs(jobs), UNGATED_AGENT_JOB)}
    assert "prod" not in ungated


def test_non_dict_entries_are_ignored():
    assert audit_jobs([None, "nope", 42, job(no_agent=True, script="s.sh")]) == []


def test_ordering_is_stable_across_runs():
    jobs = [job(id=f"j{i}", name=f"job {i}") for i in range(6)]
    assert [f.job_id for f in audit_jobs(jobs)] == [
        f.job_id for f in audit_jobs(jobs)
    ]


def test_findings_serialize_to_plain_dicts():
    d = audit_jobs([job()])[0].as_dict()
    assert set(d) == {
        "kind",
        "job_id",
        "job_name",
        "severity",
        "summary",
        "recommendation",
        "related",
    }


# ── report rendering ─────────────────────────────────────────────────────────

def test_clean_report_says_so():
    assert "check that can fail" in format_audit_report([])


def test_report_lists_each_finding_with_its_fix():
    report = format_audit_report(audit_jobs([job(prompt="recommend next steps")]))
    assert "1 finding(s)" in report
    assert "HIGH" in report
    assert "→" in report


def test_decision_marker_helper():
    assert feeds_a_decision(job(prompt="what should we do about the backlog"))
    assert not feeds_a_decision(job(prompt="list yesterday's merged PRs"))


# ── suggestions bridge ───────────────────────────────────────────────────────

def test_suggest_audit_remediations_creates_checker_suggestion(tmp_path, monkeypatch):
    import importlib
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.suggestions as store
    importlib.reload(store)

    jobs = [job(id="j1", name="Daily Summary", model="claude-3-5-sonnet", prompt="summarize PRs")]
    findings = audit_jobs(jobs)
    assert len(findings) == 1
    assert findings[0].kind == UNGATED_AGENT_JOB

    created = suggest_audit_remediations(findings, jobs)
    assert len(created) == 1
    assert created[0]["title"] == "Gate: Daily Summary checker"
    assert created[0]["source"] == "audit"
    assert created[0]["job_spec"]["context_from"] == ["j1"]
    assert created[0]["job_spec"]["model"] == "claude-3-5-haiku"

    # Repeated suggestion is deduplicated
    created_again = suggest_audit_remediations(findings, jobs)
    assert created_again == []


def test_suggest_audit_remediations_creates_demote_suggestion_for_script(tmp_path, monkeypatch):
    import importlib
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.suggestions as store
    importlib.reload(store)

    jobs = [job(id="j2", name="Disk Check", script="check_disk.sh", prompt="check if disk space is low")]
    findings = audit_jobs(jobs)
    assert len(findings) == 2  # MECHANICAL_LLM_JOB + UNGATED_AGENT_JOB

    created = suggest_audit_remediations(findings, jobs)
    demote_suggestions = [c for c in created if c["title"] == "Demote: Disk Check to script"]
    assert len(demote_suggestions) == 1
    assert demote_suggestions[0]["source"] == "audit"
    assert demote_suggestions[0]["job_spec"]["no_agent"] is True
    assert demote_suggestions[0]["job_spec"]["script"] == "check_disk.sh"
