"""Strict fail-closed tests for the label-rerun provider adapter.

Fixtures mirror the reviewed P1 schema.  These tests use deterministic
provider-shaped data only; they never call GitHub or mutate a run.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "pipeline_state.py"
_spec = importlib.util.spec_from_file_location("pipeline_state", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("failed to load pipeline_state.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

HEAD = "a" * 40
BASE = "b" * 40
OTHER = "c" * 40
APP = {"id": "15368", "slug": "github-actions"}


def _step(name="pytest", status="completed", conclusion="failure"):
    return {"name": name, "status": status, "conclusion": conclusion}


def _job(
    *,
    job_id="job-1",
    name="All required checks pass",
    status="completed",
    conclusion="failure",
    runner_id=42,
    steps=None,
    annotations=None,
):
    return {
        "job_id": job_id,
        "head_sha": HEAD,
        "attempt": 3,
        "status": status,
        "conclusion": conclusion,
        "runner_id": runner_id,
        "steps": [_step()] if steps is None else steps,
        "annotations": [] if annotations is None else annotations,
    }


def fixture(*, run_status="completed", run_conclusion="failure", jobs=None, merge_base=BASE):
    context = {
        "context": "All required checks pass",
        "head_sha": HEAD,
        "app": dict(APP),
        "attempt": 3,
        "status": run_status,
        "conclusion": run_conclusion,
        "jobs": [_job()] if jobs is None else jobs,
    }
    return {
        "schema": 1,
        "target": {
            "repository": {"owner": "example-org", "repo": "example-repo"},
            "head_sha": HEAD,
            "context": "All required checks pass",
            "provider_app": dict(APP),
            "latest_attempt": 3,
        },
        "provider_snapshot": {
            "repository": {"owner": "example-org", "repo": "example-repo"},
            "head_sha": HEAD,
            "required_context": "All required checks pass",
            "provider_app": dict(APP),
            "latest_attempt": 3,
            "workflow_run": {
                "run_id": "run-123",
                "head_sha": HEAD,
                "attempt": 3,
                "status": "completed",
                "conclusion": "failure",
            },
            "pagination": {
                "contexts_complete": True,
                "jobs_complete": True,
                "annotations_complete": True,
            },
            "contexts": [context],
        },
        "lineage": {
            "merge_base": merge_base,
            "source_branch": "feature/label-rerun",
            "target_branch": "main",
        },
        "local": {"status": "NOT_RUN", "exit_code": None, "selector": None},
        "post_write": {"head_sha": HEAD, "readback_complete": True},
    }


def _context(data):
    return data["provider_snapshot"]["contexts"][0]


def _job0(data):
    return _context(data)["jobs"][0]


def _assert_all_routing_false(result):
    assert result["routing"] == {
        "retry_allowed": False,
        "code_fix_allowed": False,
        "local_repro_required": False,
        "source_branch_loop_allowed": False,
    }


def test_p1_result_has_exact_keys_and_does_not_invent_retry_routing():
    result = _mod.diagnose_pipeline(fixture())
    assert list(result) == ["schema", "classification", "reason_codes", "routing", "evidence"]
    assert result["classification"] == "CODE_OR_TEST_FAILURE"
    assert result["routing"] == {
        "retry_allowed": False,
        "code_fix_allowed": True,
        "local_repro_required": True,
        "source_branch_loop_allowed": True,
    }
    assert _mod.can_request_rerun(fixture()) is True


def test_billing_spending_prestart_is_blocked_without_rerun():
    data = fixture(jobs=[_job(runner_id=0, steps=[], annotations=[
        {"category": "billing", "message": "provider spending limit"},
    ])])
    result = _mod.diagnose_pipeline(data)
    assert result["classification"] == "BLOCKED_PROVIDER_BILLING"
    assert result["reason_codes"] == ["BILLING_ANNOTATION"]
    _assert_all_routing_false(result)
    assert _mod.can_request_rerun(data) is False


def test_action_required_with_no_jobs_is_provider_policy_block():
    data = fixture(run_conclusion="action_required", jobs=[])
    result = _mod.diagnose_pipeline(data)
    assert result["classification"] == "BLOCKED_PROVIDER_POLICY"
    assert result["reason_codes"] == ["ACTION_REQUIRED"]
    _assert_all_routing_false(result)
    assert _mod.can_request_rerun(data) is False


def test_started_step_from_another_job_cannot_authorize_unstarted_failure():
    data = fixture(jobs=[
        _job(job_id="executed-success", conclusion="success", steps=[_step(conclusion="success")]),
        _job(job_id="unstarted-failure", runner_id=0, steps=[], conclusion="failure"),
    ])
    result = _mod.diagnose_pipeline(data)
    assert result["classification"] == "WORKFLOW_OR_HARNESS_FAILURE"
    assert result["evidence"]["real_started_step"] is False
    assert _mod.can_request_rerun(data) is False


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["provider_snapshot"]["contexts"].clear(),
        lambda d: d["provider_snapshot"]["pagination"].update({"jobs_complete": False}),
        lambda d: d["provider_snapshot"]["pagination"].update({"annotations_complete": False}),
        lambda d: d["provider_snapshot"]["contexts"][0].update({"status": "mystery"}),
    ],
)
def test_missing_unknown_or_incomplete_evidence_never_requests_rerun(mutate):
    data = fixture()
    mutate(data)
    result = _mod.diagnose_pipeline(data)
    assert result["classification"] == "EVIDENCE_INCOMPLETE"
    assert _mod.can_request_rerun(data) is False


def test_malformed_identity_types_fail_closed_without_exception():
    data = fixture()
    data["target"]["head_sha"] = 7
    result = _mod.diagnose_pipeline(data)
    assert result["classification"] == "EVIDENCE_INCOMPLETE"
    assert result["reason_codes"] == ["INVALID_INPUT_SCHEMA"]
    _assert_all_routing_false(result)


def test_blocked_lineage_precedes_failure_and_never_authorizes_rerun():
    data = fixture(merge_base=None)
    result = _mod.diagnose_pipeline(data)
    assert result["classification"] == "BLOCKED_LINEAGE"
    assert result["reason_codes"] == ["NO_MERGE_BASE"]
    _assert_all_routing_false(result)
    assert _mod.can_request_rerun(data) is False


def test_zero_synthetic_merge_base_is_not_accepted_as_real_evidence():
    data = fixture(merge_base="0" * 40)
    result = _mod.diagnose_pipeline(data)
    assert result["classification"] == "EVIDENCE_INCOMPLETE"
    assert _mod.can_request_rerun(data) is False


def test_failed_rerun_is_red_and_makes_one_command_attempt():
    decision = _mod.diagnose_pipeline(fixture())
    calls = []

    def fail_runner(args):
        calls.append(args)
        raise _mod.RerunCommandError("gh run rerun failed")

    with pytest.raises(_mod.RerunCommandError):
        _mod.request_exactly_one_rerun(
            decision, repo="example-org/example-repo", run_id="123", runner=fail_runner,
            expected_run_id="123", expected_attempt=3,
            expected_head_sha=HEAD, head_reader=lambda: HEAD,
            workflow_run_reader=lambda: {"run_id": "123", "head_sha": HEAD, "attempt": 3, "status": "completed", "conclusion": "failure"},
            required_check_reader=lambda: {"status": "completed", "conclusion": "failure", "head_sha": HEAD},
        )
    assert calls == [["run", "rerun", "123", "--repo", "example-org/example-repo", "--failed"]]


def test_successful_rerun_emits_only_rerun_requested():
    decision = _mod.diagnose_pipeline(fixture())
    result = _mod.request_exactly_one_rerun(
        decision,
        repo="example-org/example-repo",
        run_id="123",
        runner=lambda args: None,
        expected_run_id="123",
        expected_attempt=3,
        expected_head_sha=HEAD,
        head_reader=lambda: HEAD,
        workflow_run_reader=lambda: {"run_id": "123", "head_sha": HEAD, "attempt": 3, "status": "completed", "conclusion": "failure"},
        required_check_reader=lambda: {"status": "completed", "conclusion": "failure", "head_sha": HEAD},
    )
    assert result == {"status": "RERUN_REQUESTED"}


def test_exact_workflow_and_head_selection_rejects_foreign_runs():
    runs = [
        {"databaseId": 1, "workflowName": "CI", "headSha": OTHER, "status": "completed"},
        {"databaseId": 2, "workflowName": "Other", "headSha": HEAD, "status": "completed"},
        {"databaseId": 3, "workflowName": "CI", "headSha": HEAD, "status": "completed"},
    ]
    assert _mod.select_exact_workflow_run(runs, workflow="CI", head_sha=HEAD)["databaseId"] == 3
    assert _mod.select_exact_workflow_run(runs, workflow="CI", head_sha="d" * 40) is None


def _raw_run(*, attempt=4):
    return {
        "databaseId": 123,
        "headSha": HEAD,
        "workflowName": "CI",
        "status": "completed",
        "conclusion": "failure",
        "attempt": attempt,
    }


def _raw_job(job_id="job-1", name="All required checks pass", **overrides):
    return {
        "id": job_id,
        "name": name,
        "head_sha": HEAD,
        "run_attempt": 4,
        "status": "completed",
        "conclusion": "failure",
        "runner_id": 42,
        "steps": [{"name": "pytest", "status": "completed", "conclusion": "failure"}],
        **overrides,
    }


def _raw_check(check_id=9, name="All required checks pass", **overrides):
    return {"id": check_id, "name": name, "head_sha": HEAD, "status": "completed", "conclusion": "failure", "app": dict(APP), **overrides}


def test_provider_builder_binds_only_required_check_and_never_smears_annotations():
    data = _mod.build_fixture_from_api(
        repo="example-org/example-repo",
        head_sha=HEAD,
        run=_raw_run(attempt=4),
        jobs=[_raw_job()],
        check_runs=[_raw_check(8, "Foreign unrelated check"), _raw_check(9)],
        annotations_by_check={
            8: [{"category": "billing", "message": "foreign spending"}],
            9: [{"category": "code_test", "message": "required failure"}],
        },
        base_sha=BASE,
        source_branch="feature/label-rerun",
        target_branch="main",
        merge_base=BASE,
    )
    assert data is not None
    assert data["target"]["latest_attempt"] == 4
    assert [c["context"] for c in data["provider_snapshot"]["contexts"]] == ["All required checks pass"]
    assert data["provider_snapshot"]["contexts"][0]["jobs"][0]["annotations"] == [
        {"category": "code_test", "message": "required failure"}
    ]


def test_provider_builder_rejects_missing_required_check_without_synthesizing_it():
    data = _mod.build_fixture_from_api(
        repo="example-org/example-repo",
        head_sha=HEAD,
        run=_raw_run(),
        jobs=[_raw_job(name="Foreign unrelated check")],
        check_runs=[_raw_check(8, "Foreign unrelated check")],
        annotations_by_check={8: []},
        base_sha=BASE,
        source_branch="feature/label-rerun",
        target_branch="main",
        merge_base=BASE,
    )
    assert data is None


def test_post_write_readback_head_change_invalidates_old_failure():
    data = fixture()
    data["post_write"]["head_sha"] = OTHER
    result = _mod.diagnose_pipeline(data)
    assert result["classification"] == "EVIDENCE_INCOMPLETE"
    assert "POST_WRITE_HEAD_CHANGED" in result["reason_codes"]
    assert _mod.can_request_rerun(data) is False


def test_missing_provider_head_shas_are_not_substituted():
    run = _raw_run()
    jobs = [_raw_job()]
    check = _raw_check()
    check.pop("head_sha")
    assert _mod.build_fixture_from_api(
        repo="example-org/example-repo", head_sha=HEAD, run=run, jobs=jobs,
        check_runs=[check], annotations_by_check={9: []}, base_sha=BASE,
        source_branch="feature/label-rerun", target_branch="main", merge_base=BASE,
    ) is None
    check = _raw_check()
    jobs[0].pop("head_sha")
    assert _mod.build_fixture_from_api(
        repo="example-org/example-repo", head_sha=HEAD, run=run, jobs=jobs,
        check_runs=[check], annotations_by_check={9: []}, base_sha=BASE,
        source_branch="feature/label-rerun", target_branch="main", merge_base=BASE,
    ) is None


def test_required_check_status_and_conclusion_override_workflow_run_status():
    run = _raw_run()
    check = _raw_check(status="completed", conclusion="success")
    data = _mod.build_fixture_from_api(
        repo="example-org/example-repo", head_sha=HEAD, run=run, jobs=[_raw_job(conclusion="success")],
        check_runs=[check], annotations_by_check={9: []}, base_sha=BASE,
        source_branch="feature/label-rerun", target_branch="main", merge_base=BASE,
    )
    assert data is not None
    context = data["provider_snapshot"]["contexts"][0]
    assert context["status"] == "completed"
    assert context["conclusion"] == "success"
    result = _mod.diagnose_pipeline(data, require_post_write=False)
    assert result["classification"] == "EVIDENCE_INCOMPLETE"
    assert "PROVIDER_NOT_GREEN" in result["reason_codes"]
    assert _mod.can_request_rerun(data) is False


def test_selected_check_must_be_the_trusted_github_actions_app():
    check = _raw_check(app={"id": "foreign", "slug": "foreign-provider"})
    data = _mod.build_fixture_from_api(
        repo="example-org/example-repo", head_sha=HEAD, run=_raw_run(), jobs=[_raw_job()],
        check_runs=[check], annotations_by_check={9: []}, base_sha=BASE,
        source_branch="feature/label-rerun", target_branch="main", merge_base=BASE,
    )
    assert data is None


def test_any_billing_or_spending_annotation_blocks_even_executed_failure():
    data = fixture(jobs=[_job(runner_id=42, steps=[_step()], annotations=[
        {"category": "billing", "message": "spending limit"},
    ])])
    result = _mod.diagnose_pipeline(data)
    assert result["classification"] == "BLOCKED_PROVIDER_BILLING"
    assert _mod.can_request_rerun(data) is False


def test_paginated_response_accepts_rest_wrappers_and_rejects_non_objects():
    item = {"id": 1}
    assert _mod.parse_paginated_response({"jobs": [item]}, "jobs") == [item]
    assert _mod.parse_paginated_response({"check_runs": [item]}, "check_runs") == [item]
    assert _mod.parse_paginated_response({"jobs": [item, "HOSTILE_NON_OBJECT"]}, "jobs") is None
    assert _mod.parse_paginated_response([{"jobs": [item]}, {"jobs": ["HOSTILE_NON_OBJECT"]}], "jobs") is None


def test_each_job_run_attempt_must_equal_the_exact_run_attempt():
    jobs = [_raw_job(run_attempt=3)]
    assert _mod.build_fixture_from_api(
        repo="example-org/example-repo", head_sha=HEAD, run=_raw_run(attempt=4), jobs=jobs,
        check_runs=[_raw_check()], annotations_by_check={9: []}, base_sha=BASE,
        source_branch="feature/label-rerun", target_branch="main", merge_base=BASE,
    ) is None
    jobs = [_raw_job()]
    jobs[0].pop("run_attempt")
    assert _mod.build_fixture_from_api(
        repo="example-org/example-repo", head_sha=HEAD, run=_raw_run(attempt=4), jobs=jobs,
        check_runs=[_raw_check()], annotations_by_check={9: []}, base_sha=BASE,
        source_branch="feature/label-rerun", target_branch="main", merge_base=BASE,
    ) is None


def test_real_ci_yaml_failed_aggregate_path_binds_annotations_to_aggregate_job():
    jobs = [
        _raw_job(job_id="unit", name="Python tests", conclusion="success", steps=[_step(conclusion="success")]),
        _raw_job(job_id="aggregate", name="All required checks pass"),
    ]
    data = _mod.build_fixture_from_api(
        repo="example-org/example-repo", head_sha=HEAD, run=_raw_run(), jobs=jobs,
        check_runs=[_raw_check(9, "All required checks pass")],
        annotations_by_check={9: [{"message": "::error::1 job failed"}]}, base_sha=BASE,
        source_branch="feature/label-rerun", target_branch="main", merge_base=BASE,
    )
    assert data is not None
    bound_jobs = data["provider_snapshot"]["contexts"][0]["jobs"]
    assert bound_jobs[0]["annotations"] == []
    assert bound_jobs[1]["annotations"] == [{"category": "code_test", "message": "::error::1 job failed"}]
    assert _mod.can_request_rerun(data) is True


def test_rerun_rechecks_original_head_immediately_before_command():
    decision = _mod.diagnose_pipeline(fixture())
    calls = []
    with pytest.raises(_mod.RerunCommandError):
        _mod.request_exactly_one_rerun(
            decision, repo="example-org/example-repo", run_id="123", runner=lambda args: calls.append(args),
            expected_head_sha=HEAD, head_reader=lambda: OTHER,
            expected_run_id="123", expected_attempt=3,
            workflow_run_reader=lambda: {"run_id": "123", "head_sha": HEAD, "attempt": 3, "status": "completed", "conclusion": "failure"},
            required_check_reader=lambda: {"status": "completed", "conclusion": "failure", "head_sha": HEAD},
    )
    assert calls == []


def test_post_mutation_readback_requires_exact_head_and_run():
    assert _mod.verify_exact_head_run_readback(
        expected_head_sha=HEAD, expected_run_id="123",
        head_reader=lambda: HEAD, run_reader=lambda: {"databaseId": 123, "headSha": HEAD},
    ) is True
    assert _mod.verify_exact_head_run_readback(
        expected_head_sha=HEAD, expected_run_id="123",
        head_reader=lambda: OTHER, run_reader=lambda: {"databaseId": 123, "headSha": HEAD},
    ) is False


@pytest.mark.parametrize(
    "status,conclusion",
    [("completed", "success"), ("in_progress", None)],
)
def test_non_failed_required_check_cannot_enter_annotation_or_rerun_route(status, conclusion):
    data = fixture(run_status=status, run_conclusion=conclusion, jobs=[_job(annotations=[
        {"category": "code_test", "message": "ordinary failure annotation"},
    ])])
    result = _mod.diagnose_pipeline(data, require_post_write=False)
    assert result["classification"] != "CODE_OR_TEST_FAILURE"
    assert _mod.can_request_rerun(data) is False


def test_mixed_paginated_wrapper_and_bare_items_are_rejected():
    item = {"id": 1}
    assert _mod.parse_paginated_response([{"jobs": [item]}, item], "jobs") is None
    assert _mod.parse_paginated_response([{"jobs": [item]}, {"check_runs": [item]}], "jobs") is None
    assert _mod.parse_paginated_response([{"jobs": [item]}, {"jobs": ["bad"]}], "jobs") is None


def test_paginated_response_rejects_mixed_wrapper_keys_in_one_shape():
    item = {"id": 1}
    assert _mod.parse_paginated_response({"jobs": [item], "check_runs": []}, "jobs") is None
    assert _mod.parse_paginated_response([{"jobs": [item], "check_runs": []}], "jobs") is None


def test_aggregate_job_state_must_match_selected_required_check():
    check = _raw_check(status="completed", conclusion="failure")
    jobs = [_raw_job(status="completed", conclusion="success")]
    assert _mod.build_fixture_from_api(
        repo="example-org/example-repo", head_sha=HEAD, run=_raw_run(), jobs=jobs,
        check_runs=[check], annotations_by_check={9: []}, base_sha=BASE,
        source_branch="feature/label-rerun", target_branch="main", merge_base=BASE,
    ) is None


def test_truthy_non_boolean_post_write_flag_fails_closed():
    data = fixture()
    data["post_write"]["readback_complete"] = "HOSTILE_TRUTHY"
    result = _mod.diagnose_pipeline(data)
    assert result["classification"] == "EVIDENCE_INCOMPLETE"
    assert result["reason_codes"] == ["INVALID_INPUT_SCHEMA"]
    assert _mod.can_request_rerun(data) is False


def test_mutation_authorization_reasserts_required_check_failure():
    decision = _mod.diagnose_pipeline(fixture())
    calls = []
    with pytest.raises(_mod.RerunCommandError):
        _mod.request_exactly_one_rerun(
            decision, repo="example-org/example-repo", run_id="123",
            runner=lambda args: calls.append(args), eligible=True,
            expected_head_sha=HEAD, head_reader=lambda: HEAD,
            expected_run_id="123", expected_attempt=3,
            workflow_run_reader=lambda: {"run_id": "123", "head_sha": HEAD, "attempt": 3, "status": "completed", "conclusion": "failure"},
            required_check_reader=lambda: {"status": "completed", "conclusion": "success", "head_sha": HEAD},
        )
    assert calls == []


def test_mutation_without_fresh_head_and_required_check_fences_is_rejected():
    decision = _mod.diagnose_pipeline(fixture())
    calls = []
    with pytest.raises(_mod.RerunCommandError):
        _mod.request_exactly_one_rerun(
            decision, repo="example-org/example-repo", run_id="123",
            runner=lambda args: calls.append(args), eligible=True,
        )
    assert calls == []


@pytest.mark.parametrize(
    "status,conclusion",
    [("in_progress", None), ("mystery", "failure"), ("completed", "success")],
)
def test_target_workflow_run_state_must_be_completed_failure(status, conclusion):
    data = fixture()
    data["provider_snapshot"]["workflow_run"].update({"status": status, "conclusion": conclusion})
    result = _mod.diagnose_pipeline(data, require_post_write=False)
    assert result["classification"] == "EVIDENCE_INCOMPLETE"
    assert _mod.can_request_rerun(data) is False


def test_builder_carries_exact_workflow_run_identity_and_state():
    data = _mod.build_fixture_from_api(
        repo="example-org/example-repo", head_sha=HEAD, run=_raw_run(attempt=4), jobs=[_raw_job()],
        check_runs=[_raw_check()], annotations_by_check={9: []}, base_sha=BASE,
        source_branch="feature/label-rerun", target_branch="main", merge_base=BASE,
    )
    assert data is not None
    assert data["provider_snapshot"]["workflow_run"] == {
        "run_id": "123", "head_sha": HEAD, "attempt": 4,
        "status": "completed", "conclusion": "failure",
    }


@pytest.mark.parametrize(
    "mutate",
    [
        {"run_id": "other", "head_sha": HEAD, "attempt": 4, "status": "completed", "conclusion": "failure"},
        {"run_id": "123", "head_sha": OTHER, "attempt": 4, "status": "completed", "conclusion": "failure"},
        {"run_id": "123", "head_sha": HEAD, "attempt": 2, "status": "completed", "conclusion": "failure"},
        {"run_id": "123", "head_sha": HEAD, "attempt": 4, "status": "in_progress", "conclusion": None},
        {"run_id": "123", "head_sha": HEAD, "attempt": 4, "status": "completed", "conclusion": "success"},
    ],
)
def test_mutation_rejects_changed_workflow_run_identity_or_state(mutate):
    decision = _mod.diagnose_pipeline(fixture())
    calls = []
    with pytest.raises(_mod.RerunCommandError):
        _mod.request_exactly_one_rerun(
            decision, repo="example-org/example-repo", run_id="123",
            runner=lambda args: calls.append(args), eligible=True,
            expected_run_id="123", expected_attempt=3, expected_head_sha=HEAD,
            head_reader=lambda: HEAD,
            workflow_run_reader=lambda: mutate,
            required_check_reader=lambda: {"status": "completed", "conclusion": "failure", "head_sha": HEAD},
        )
    assert calls == []


def test_collection_rejects_detail_run_id_mismatch_before_downstream_calls(monkeypatch):
    downstream_calls = []

    def fake_gh(args):
        if args[:3] == ["pr", "view", "77"]:
            return {
                "headRefOid": HEAD,
                "baseRefOid": BASE,
                "headRefName": "feature/label-rerun",
                "baseRefName": "main",
            }
        if args[:2] == ["run", "list"]:
            return {
                "workflow_runs": [{
                    "databaseId": 123,
                    "status": "completed",
                    "conclusion": "failure",
                    "headSha": HEAD,
                    "workflowName": "CI",
                    "createdAt": "2026-08-29T18:00:00Z",
                }]
            }
        if args[:3] == ["run", "view", "123"]:
            return {
                "databaseId": 999,
                "status": "completed",
                "conclusion": "failure",
                "headSha": HEAD,
                "workflowName": "CI",
                "attempt": 4,
            }
        downstream_calls.append(args)
        if args[:3] == ["api", "--paginate", "--slurp"] and "/actions/runs/123/jobs" in args[3]:
            return {"jobs": [_raw_job()]}
        if args[:3] == ["api", "--paginate", "--slurp"] and "/check-runs?" in args[3]:
            return {"check_runs": [_raw_check()]}
        if args[:3] == ["api", "--paginate", "--slurp"] and "/check-runs/9/annotations" in args[3]:
            return {"annotations": []}
        if args[:2] == ["api", f"repos/example-org/example-repo/compare/{BASE}...{HEAD}"]:
            return {"merge_base_commit": {"sha": BASE}}
        raise AssertionError(f"unexpected provider call: {args}")

    monkeypatch.setattr(_mod, "_gh_json", fake_gh)
    with pytest.raises(_mod.EvidenceError):
        _mod.collect_provider_fixture(
            repo="example-org/example-repo",
            pr_number="77",
            workflow="CI",
            event_head_sha=HEAD,
        )
    assert downstream_calls == []


def test_mutation_target_run_id_substitution_is_rejected_before_runner():
    decision = _mod.diagnose_pipeline(fixture())
    calls = []
    with pytest.raises(_mod.RerunCommandError):
        _mod.request_exactly_one_rerun(
            decision,
            repo="example-org/example-repo",
            run_id="ATTACKER-CHANGED-RUN",
            runner=lambda args: calls.append(args),
            eligible=True,
            expected_run_id="123",
            expected_attempt=3,
            expected_head_sha=HEAD,
            head_reader=lambda: HEAD,
            workflow_run_reader=lambda: {
                "run_id": "123",
                "head_sha": HEAD,
                "attempt": 3,
                "status": "completed",
                "conclusion": "failure",
            },
            required_check_reader=lambda: {
                "status": "completed",
                "conclusion": "failure",
                "head_sha": HEAD,
            },
        )
    assert calls == []


@pytest.mark.parametrize(
    "status,conclusion",
    [("in_progress", None), ("unknown", "mystery"), ("completed", "success")],
)
def test_bounded_builder_rejects_non_completed_failure_run_state(status, conclusion):
    run = _raw_run()
    run.update({"status": status, "conclusion": conclusion})
    assert _mod.build_fixture_from_api(
        repo="example-org/example-repo",
        head_sha=HEAD,
        run=run,
        jobs=[_raw_job()],
        check_runs=[_raw_check()],
        annotations_by_check={9: []},
        base_sha=BASE,
        source_branch="feature/label-rerun",
        target_branch="main",
        merge_base=BASE,
    ) is None
