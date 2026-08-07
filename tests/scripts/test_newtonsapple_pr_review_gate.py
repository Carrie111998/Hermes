"""Contract tests for the downtime-tolerant NewtonsApple review gate."""

import json
import base64
import hashlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts import newtonsapple_pr_review_gate as gate
from scripts.newtonsapple_pr_review_gate import (
    ReviewStateStore,
    ReviewTuple,
    TrustedWorkflow,
    _buzz_find,
    _gate_webhook,
    _workflow_from_environment,
    select_authorized_tuple,
    drain_summary_outbox,
    parse_status_context,
    validate_capture_status,
)


def test_capture_workflow_identity_is_code_pinned_not_environment_controlled(
    monkeypatch,
):
    monkeypatch.setenv("NEWTONSAPPLE_REVIEW_CAPTURE_WORKFLOW_ID", "999")
    monkeypatch.setenv(
        "NEWTONSAPPLE_REVIEW_CAPTURE_WORKFLOW_PATH",
        ".github/workflows/attacker-controlled.yml",
    )

    workflow = _workflow_from_environment()

    assert workflow == TrustedWorkflow(
        workflow_id=328661288,
        path=".github/workflows/pr-review-capture.yml",
        branch="dev",
    )


BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


def _execution_request(operation):
    return {
        "operation": operation,
        "contract_version": "v2",
        "repository": "NewtonsAppleAI/newtonsapple-web",
        "pr_number": "185",
        "base_sha": BASE_SHA,
        "head_sha": HEAD_SHA,
    }


def _trusted_ci_fixture():
    run = {
        "id": 31035273202,
        "workflow_id": gate.TRUSTED_EXECUTION_WORKFLOW_ID,
        "path": gate.TRUSTED_EXECUTION_WORKFLOW_PATH,
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "head_branch": "dev",
        "head_sha": "c" * 40,
        "display_title": (
            "pr-review-execution-v2/dispatch/event-29064129383/pr-185/"
            f"base-{BASE_SHA}/head-{HEAD_SHA}"
        ),
        "actor": {"login": "bas4r"},
    }
    jobs = [
        {
            "id": index,
            "name": name,
            "status": "completed",
            "conclusion": "success",
            "started_at": "2026-08-05T18:00:00Z",
            "completed_at": "2026-08-05T18:01:00Z",
            "html_url": (
                "https://github.com/NewtonsAppleAI/newtonsapple-web/"
                f"actions/runs/31035273202/job/{index}"
            ),
            "labels": ["ubuntu-latest"],
            "steps": [
                {
                    "name": f"Attest clean {name if name != 'e2e' else 'E2E'} tree before gate",
                    "conclusion": "success",
                },
                {
                    "name": {
                        "quality": "Run the shared quality harness",
                        "integration": "Replay PostgreSQL and run all integration contracts",
                        "e2e": "Run all release journeys",
                    }[name],
                    "conclusion": "success",
                },
                {
                    "name": f"Attest clean {name if name != 'e2e' else 'E2E'} tree after gate",
                    "conclusion": "success",
                },
            ],
        }
        for index, name in enumerate(("quality", "integration", "e2e"), 101)
    ]
    return run, jobs


def test_gate_contract_rejects_failure_after_a_successful_gate():
    _, jobs = _trusted_ci_fixture()
    quality = jobs[0]
    quality["conclusion"] = "failure"

    with pytest.raises(RuntimeError, match="job conclusion does not match gate"):
        gate._gate_contract(quality)


def _live_execution_pr():
    return {
        "number": 185,
        "state": "open",
        "draft": False,
        "base": {"ref": "dev", "sha": BASE_SHA},
        "head": {"sha": HEAD_SHA},
        "requested_reviewers": [{"login": "newtonsapple-bot"}],
    }


def _execution_log(gate_name, tree_sha):
    digest = gate.EXECUTION_GATE_COMMAND_SHA256[gate_name]
    marker = (
        f"newtonsapple-review-execution-v2 gate={gate_name} head={HEAD_SHA} "
        f"tree={tree_sha} command_sha256={digest}"
    )
    return f"{marker} phase=before\n{marker} phase=after\n".encode()


def _install_attestation_key(monkeypatch):
    private_key = Ed25519PrivateKey.generate()
    raw = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    monkeypatch.setenv(
        "NEWTONSAPPLE_REVIEW_ATTESTATION_PRIVATE_KEY",
        base64.b64encode(raw).decode(),
    )
    return private_key


def test_gate_resolution_is_signed_and_bound_to_exact_trusted_ci(monkeypatch):
    private_key = _install_attestation_key(monkeypatch)
    run, jobs = _trusted_ci_fixture()
    with patch.object(gate, "_trusted_ci_evidence", return_value=(run, jobs)):
        result = gate.resolve_execution_gates(
            _execution_request("resolve_execution_gates")
        )

    payload = base64.b64decode(result["gate_resolution_payload"])
    private_key.public_key().verify(
        base64.b64decode(result["gate_resolution_signature"]), payload
    )
    manifest = json.loads(payload)
    assert manifest["resolved_gates"] == ["quality", "integration", "e2e"]
    assert manifest["policy_sha256"] == gate.EXECUTION_GATE_POLICY_SHA256
    assert manifest["gate_contracts"]["quality"] == {
        "kind": "command",
        "command": ["npm", "run", "check"],
        "executor": "github_actions",
        "runner": {"kind": "github_actions", "name": "ubuntu-latest"},
        "status": "pass",
        "exit_codes": [0],
    }


def test_gate_resolution_canonicalizes_unordered_github_job_inventory(monkeypatch):
    _install_attestation_key(monkeypatch)
    run, jobs = _trusted_ci_fixture()
    jobs = [jobs[2], jobs[1], jobs[0]]

    canonical = gate._canonical_ci_jobs(jobs)
    assert [job["name"] for job in canonical] == ["quality", "integration", "e2e"]

    with patch.object(gate, "_trusted_ci_evidence", return_value=(run, jobs)):
        result = gate.resolve_execution_gates(
            _execution_request("resolve_execution_gates")
        )

    manifest = json.loads(base64.b64decode(result["gate_resolution_payload"]))
    assert manifest["resolved_gates"] == ["quality", "integration", "e2e"]
    assert set(manifest["gate_contracts"]) == {"quality", "integration", "e2e"}


def test_execution_evidence_hashes_exact_logs_and_omits_unused_worker(monkeypatch):
    private_key = _install_attestation_key(monkeypatch)
    run, jobs = _trusted_ci_fixture()
    head_tree_sha = "d" * 40
    logs = {
        job["id"]: _execution_log(job["name"], head_tree_sha) for job in jobs
    }
    with (
        patch.object(gate, "_trusted_ci_evidence", return_value=(run, jobs)),
        patch.object(
            gate, "_commit_tree_sha", side_effect=["c" * 40, head_tree_sha]
        ),
        patch.object(gate, "_job_log", side_effect=lambda job_id: logs[job_id]),
    ):
        result = gate.execution_evidence(_execution_request("execution_evidence"))

    payload = base64.b64decode(result["attestation_payload"])
    private_key.public_key().verify(
        base64.b64decode(result["attestation_signature"]), payload
    )
    report = json.loads(payload)
    assert report["worker"] == {"required": False}
    assert [item["id"] for item in report["gates"]] == [
        "quality",
        "integration",
        "e2e",
    ]
    assert report["gates"][0]["evidence"]["log_sha256"] == hashlib.sha256(
        logs[101]
    ).hexdigest()


def test_trusted_execution_rejects_pull_request_merge_ref_run(monkeypatch):
    run, jobs = _trusted_ci_fixture()
    run.update(
        event="pull_request",
        head_sha=HEAD_SHA,
        display_title="CI",
        pull_requests=[
            {
                "number": 185,
                "base": {"sha": BASE_SHA},
                "head": {"sha": HEAD_SHA},
            }
        ],
    )
    monkeypatch.setenv("NEWTONSAPPLE_REVIEW_BOT_LOGIN", "newtonsapple-bot")
    with (
        patch.object(gate, "gh_json", return_value=_live_execution_pr()),
        patch.object(gate, "_collection", return_value=[run]),
    ):
        with pytest.raises(RuntimeError, match="exact-head execution run"):
            gate._trusted_ci_evidence(
                ReviewTuple(
                    repository=gate.REPOSITORY,
                    pr_number=185,
                    base_sha=BASE_SHA,
                    head_sha=HEAD_SHA,
                )
            )


def test_trusted_execution_rejects_a_different_base_branch(monkeypatch):
    live_pr = _live_execution_pr()
    live_pr["base"]["ref"] = "staging"
    run, _ = _trusted_ci_fixture()
    monkeypatch.setenv("NEWTONSAPPLE_REVIEW_BOT_LOGIN", "newtonsapple-bot")
    with (
        patch.object(gate, "gh_json", return_value=live_pr),
        patch.object(gate, "_collection", return_value=[run]),
    ):
        with pytest.raises(RuntimeError, match="not eligible"):
            gate._trusted_ci_evidence(
                ReviewTuple(
                    repository=gate.REPOSITORY,
                    pr_number=185,
                    base_sha=BASE_SHA,
                    head_sha=HEAD_SHA,
                )
            )


def test_trusted_execution_accepts_only_pinned_dispatch_workflow(monkeypatch):
    run, jobs = _trusted_ci_fixture()
    monkeypatch.setenv("NEWTONSAPPLE_REVIEW_BOT_LOGIN", "newtonsapple-bot")
    with (
        patch.object(
            gate,
            "gh_json",
            side_effect=[_live_execution_pr(), {"jobs": jobs}],
        ),
        patch.object(gate, "_collection", return_value=[run]),
        patch.object(
            gate,
            "_trusted_execution_workflow_sha256",
            return_value=gate.TRUSTED_EXECUTION_WORKFLOW_SHA256,
        ) as workflow_digest,
    ):
        selected_run, selected_jobs = gate._trusted_ci_evidence(
            ReviewTuple(
                repository=gate.REPOSITORY,
                pr_number=185,
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
            )
        )

    assert selected_run == run
    assert selected_jobs == jobs
    workflow_digest.assert_called_once_with(run["head_sha"])


def test_trusted_execution_accepts_a_completed_pr_gate_failure(monkeypatch):
    run, jobs = _trusted_ci_fixture()
    run["conclusion"] = "failure"
    jobs[0]["conclusion"] = "failure"
    jobs[0]["steps"][1]["conclusion"] = "failure"
    monkeypatch.setenv("NEWTONSAPPLE_REVIEW_BOT_LOGIN", "newtonsapple-bot")
    with (
        patch.object(
            gate,
            "gh_json",
            side_effect=[_live_execution_pr(), {"jobs": jobs}],
        ),
        patch.object(gate, "_collection", return_value=[run]),
        patch.object(
            gate,
            "_trusted_execution_workflow_sha256",
            return_value=gate.TRUSTED_EXECUTION_WORKFLOW_SHA256,
        ),
    ):
        selected_run, selected_jobs = gate._trusted_ci_evidence(
            ReviewTuple(
                repository=gate.REPOSITORY,
                pr_number=185,
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
            )
        )

    assert selected_run == run
    assert gate._gate_contract(selected_jobs[0])["status"] == "pr-fail"


def test_trusted_execution_accepts_pinned_request_workflow_for_automatic_reviews(
    monkeypatch,
):
    run, jobs = _trusted_ci_fixture()
    run.update(
        event="pull_request_target",
        actor={"login": "contributor"},
        display_title=(
            f"pr-review-execution-v2/request/pr-185/base-{BASE_SHA}/head-{HEAD_SHA}"
        ),
        pull_requests=[
            {
                "number": 185,
                "base": {"sha": BASE_SHA},
                "head": {"sha": HEAD_SHA},
            }
        ],
    )
    monkeypatch.setenv("NEWTONSAPPLE_REVIEW_BOT_LOGIN", "newtonsapple-bot")
    with (
        patch.object(
            gate,
            "gh_json",
            side_effect=[_live_execution_pr(), {"jobs": jobs}],
        ),
        patch.object(gate, "_collection", return_value=[run]),
        patch.object(
            gate,
            "_trusted_execution_workflow_sha256",
            return_value=gate.TRUSTED_EXECUTION_WORKFLOW_SHA256,
        ),
    ):
        selected_run, selected_jobs = gate._trusted_ci_evidence(
            ReviewTuple(
                repository=gate.REPOSITORY,
                pr_number=185,
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
            )
        )

    assert selected_run == run
    assert selected_jobs == jobs


def test_trusted_execution_rejects_legacy_unscoped_run_name(monkeypatch):
    run, _ = _trusted_ci_fixture()
    run["display_title"] = (
        f"pr-review-execution-v2/pr-185/base-{BASE_SHA}/head-{HEAD_SHA}"
    )
    monkeypatch.setenv("NEWTONSAPPLE_REVIEW_BOT_LOGIN", "newtonsapple-bot")
    with (
        patch.object(gate, "gh_json", return_value=_live_execution_pr()),
        patch.object(gate, "_collection", return_value=[run]),
    ):
        with pytest.raises(RuntimeError, match="exact-head execution run"):
            gate._trusted_ci_evidence(
                ReviewTuple(
                    repository=gate.REPOSITORY,
                    pr_number=185,
                    base_sha=BASE_SHA,
                    head_sha=HEAD_SHA,
                )
            )


def test_execution_evidence_requires_recorded_exact_head_before_and_after(monkeypatch):
    _install_attestation_key(monkeypatch)
    run, jobs = _trusted_ci_fixture()
    tree_sha = "d" * 40
    logs = {job["id"]: _execution_log(job["name"], tree_sha) for job in jobs}
    with (
        patch.object(gate, "_trusted_ci_evidence", return_value=(run, jobs)),
        patch.object(gate, "_commit_tree_sha", side_effect=["c" * 40, tree_sha]),
        patch.object(gate, "_job_log", side_effect=lambda job_id: logs[job_id]),
    ):
        result = gate.execution_evidence(_execution_request("execution_evidence"))

    report = json.loads(base64.b64decode(result["attestation_payload"]))
    assert report["gates"][0]["command"] == gate.EXECUTION_GATE_COMMANDS["quality"]
    assert report["gates"][0]["tree_before"] == tree_sha
    assert report["gates"][0]["tree_after"] == tree_sha

    logs[101] = logs[101].replace(b"phase=after", b"phase=missing", 1)
    with (
        patch.object(gate, "_trusted_ci_evidence", return_value=(run, jobs)),
        patch.object(gate, "_commit_tree_sha", side_effect=["c" * 40, tree_sha]),
        patch.object(gate, "_job_log", side_effect=lambda job_id: logs[job_id]),
    ):
        with pytest.raises(RuntimeError, match="exact-head provenance"):
            gate.execution_evidence(_execution_request("execution_evidence"))


def test_status_context_parses_pr_and_base_while_target_sha_supplies_head():
    parsed = parse_status_context(
        f"newtonsapple-bot/review-v2/pr-185/base-{BASE_SHA}",
        HEAD_SHA,
    )

    assert parsed == ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        contract_version="v2",
    )


@pytest.mark.parametrize(
    ("context", "head_sha"),
    [
        (f"newtonsapple-bot/review-v2/pr-0/base-{BASE_SHA}", HEAD_SHA),
        (f"newtonsapple-bot/review-v2/pr-0185/base-{BASE_SHA}", HEAD_SHA),
        (f"newtonsapple-bot/review-v2/pr-185/base-{'A' * 40}", HEAD_SHA),
        (f"newtonsapple-bot/review-v2/pr-185/base-{BASE_SHA}/extra", HEAD_SHA),
        (f"newtonsapple-bot/review-v1/pr-185/base-{BASE_SHA}", HEAD_SHA),
        (f"newtonsapple-bot/review-v2/pr-185/base-{BASE_SHA}", "not-a-sha"),
    ],
)
def test_status_context_rejects_noncanonical_or_incomplete_tuples(context, head_sha):
    with pytest.raises(ValueError, match="status context"):
        parse_status_context(context, head_sha)


def _pending_status(**overrides):
    status = {
        "id": 901,
        "state": "pending",
        "context": f"newtonsapple-bot/review-v2/pr-185/base-{BASE_SHA}",
        "target_url": "https://github.com/NewtonsAppleAI/newtonsapple-web/actions/runs/31035273202",
        "creator": {"login": "github-actions[bot]"},
    }
    status.update(overrides)
    return status


def _capture_run(**overrides):
    run = {
        "id": 31035273202,
        "html_url": "https://github.com/NewtonsAppleAI/newtonsapple-web/actions/runs/31035273202",
        "workflow_id": 778899,
        "path": ".github/workflows/pr-review-capture.yml",
        "display_title": (
            f"pr-review-capture-v2/request/pr-185/base-{BASE_SHA}/head-{HEAD_SHA}"
        ),
        "event": "pull_request_target",
        "status": "completed",
        "conclusion": "success",
        "head_branch": "dev",
        "head_sha": BASE_SHA,
        "pull_requests": [
            {
                "number": 185,
                "base": {"sha": BASE_SHA},
                "head": {"sha": HEAD_SHA},
            }
        ],
    }
    run.update(overrides)
    return run


def _dispatch_run(**overrides):
    run = _capture_run(
        event="workflow_dispatch",
        head_sha="c" * 40,
        pull_requests=[],
        actor={"login": "bas4r"},
        display_title=(
            "pr-review-capture-v2/dispatch/event-29064129383/pr-185/"
            f"base-{BASE_SHA}/head-{HEAD_SHA}"
        ),
    )
    run.update(overrides)
    return run


def test_pending_status_is_authorized_only_by_matching_successful_allowlisted_run():
    trusted = TrustedWorkflow(
        workflow_id=778899,
        path=".github/workflows/pr-review-capture.yml",
        branch="dev",
    )

    review_tuple = validate_capture_status(
        _pending_status(),
        head_sha=HEAD_SHA,
        run=_capture_run(),
        trusted_workflow=trusted,
    )

    assert review_tuple.pr_number == 185
    assert review_tuple.base_sha == BASE_SHA
    assert review_tuple.head_sha == HEAD_SHA


def test_capture_run_loader_requires_the_pinned_base_workflow_source(monkeypatch):
    run = {
        "id": 123,
        "head_sha": BASE_SHA,
        "path": ".github/workflows/pr-review-capture.yml",
    }
    monkeypatch.setattr(gate, "gh_json", lambda *args: run)
    monkeypatch.setattr(
        gate,
        "_workflow_source_sha256",
        lambda commit_sha, path: "0" * 64,
    )

    assert gate._load_run(123) is None


def test_capture_run_loader_accepts_the_pinned_base_workflow_source(monkeypatch):
    run = {
        "id": 123,
        "head_sha": BASE_SHA,
        "path": ".github/workflows/pr-review-capture.yml",
    }
    monkeypatch.setattr(gate, "gh_json", lambda *args: run)
    monkeypatch.setattr(
        gate,
        "_workflow_source_sha256",
        lambda commit_sha, path: gate.TRUSTED_CAPTURE_WORKFLOW_SHA256,
    )

    assert gate._load_run(123) == run


def test_workflow_source_hash_accepts_github_wrapped_base64(monkeypatch):
    content = b"name: trusted\non: workflow_dispatch\n"
    encoded = base64.b64encode(content).decode("ascii")
    wrapped = "\n".join(encoded[index : index + 12] for index in range(0, len(encoded), 12)) + "\n"
    monkeypatch.setattr(
        gate,
        "gh_json",
        lambda *args: {"type": "file", "encoding": "base64", "content": wrapped},
    )

    assert gate._workflow_source_sha256(BASE_SHA, ".github/workflows/review.yml") == hashlib.sha256(content).hexdigest()


def test_dispatch_status_requires_canonical_run_name_and_allowlisted_actor():
    trusted = TrustedWorkflow(
        workflow_id=778899,
        path=".github/workflows/pr-review-capture.yml",
        branch="dev",
        allowed_dispatchers=("bas4r",),
    )

    review_tuple = validate_capture_status(
        _pending_status(),
        head_sha=HEAD_SHA,
        run=_dispatch_run(),
        trusted_workflow=trusted,
    )

    assert review_tuple.pr_number == 185


@pytest.mark.parametrize(
    "run_overrides",
    [
        {"actor": {"login": "attacker"}},
        {"display_title": "pr-review-capture-v2/dispatch/event-0/pr-185"},
        {"display_title": (
            "pr-review-capture-v2/dispatch/event-29064129383/pr-186/"
            f"base-{BASE_SHA}/head-{HEAD_SHA}"
        )},
        {"display_title": (
            "pr-review-capture-v2/dispatch/event-29064129383/pr-185/"
            f"base-{'c' * 40}/head-{HEAD_SHA}"
        )},
        {"display_title": (
            "pr-review-capture-v2/dispatch/event-29064129383/pr-185/"
            f"base-{BASE_SHA}/head-{'c' * 40}"
        )},
    ],
)
def test_dispatch_status_fails_closed_on_actor_or_run_name_mismatch(run_overrides):
    trusted = TrustedWorkflow(
        workflow_id=778899,
        path=".github/workflows/pr-review-capture.yml",
        branch="dev",
        allowed_dispatchers=("bas4r",),
    )

    with pytest.raises(ValueError, match="capture status"):
        validate_capture_status(
            _pending_status(),
            head_sha=HEAD_SHA,
            run=_dispatch_run(**run_overrides),
            trusted_workflow=trusted,
        )


@pytest.mark.parametrize(
    ("status_overrides", "run_overrides"),
    [
        ({"state": "success"}, {}),
        ({"creator": {"login": "attacker"}}, {}),
        ({"target_url": "https://example.com/actions/runs/31035273202"}, {}),
        ({"target_url": "https://github.com/NewtonsAppleAI/newtonsapple-web/actions/runs/7"}, {}),
        ({}, {"workflow_id": 1}),
        ({}, {"path": ".github/workflows/other.yml"}),
        ({}, {"event": "pull_request"}),
        ({}, {"conclusion": "failure"}),
        ({}, {"head_branch": "main"}),
        ({}, {"head_sha": "c" * 40}),
        ({}, {"pull_requests": []}),
        ({}, {"pull_requests": [{"number": 185, "base": {"sha": BASE_SHA}, "head": {"sha": "c" * 40}}]}),
    ],
)
def test_capture_status_fails_closed_on_provenance_or_tuple_mismatch(
    status_overrides, run_overrides
):
    trusted = TrustedWorkflow(
        workflow_id=778899,
        path=".github/workflows/pr-review-capture.yml",
        branch="dev",
    )

    with pytest.raises(ValueError, match="capture status"):
        validate_capture_status(
            _pending_status(**status_overrides),
            head_sha=HEAD_SHA,
            run=_capture_run(**run_overrides),
            trusted_workflow=trusted,
        )


def test_review_state_store_uses_wal_and_reclaims_only_expired_leases(tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    review_tuple = ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )

    assert store.journal_mode() == "wal"
    first_token = store.reserve(review_tuple, now=100, lease_seconds=30)
    assert isinstance(first_token, str)
    assert store.reserve(review_tuple, now=129, lease_seconds=30) is None
    second_token = store.reserve(review_tuple, now=130, lease_seconds=30)
    assert isinstance(second_token, str)
    assert second_token != first_token

    with pytest.raises(ValueError, match="lease not found"):
        store.release(review_tuple, lease_token=first_token)
    store.release(review_tuple, lease_token=second_token)
    third_token = store.reserve(review_tuple, now=131, lease_seconds=30)
    assert isinstance(third_token, str)
    store.complete(review_tuple, lease_token=third_token, now=132)
    assert store.reserve(review_tuple, now=10_000, lease_seconds=30) is None


def test_publication_claim_is_single_use_token_fenced_and_extends_lease(tmp_path):
    first_store = ReviewStateStore(tmp_path / "review.sqlite3")
    second_store = ReviewStateStore(tmp_path / "review.sqlite3")
    review_tuple = ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )
    lease_token = first_store.reserve(review_tuple, now=100, lease_seconds=30)
    assert isinstance(lease_token, str)

    first_store.claim_publication(
        review_tuple,
        lease_token=lease_token,
        now=120,
        lease_seconds=300,
    )

    assert second_store.reserve(review_tuple, now=131, lease_seconds=30) is None
    with pytest.raises(ValueError, match="review publication claim not found"):
        first_store.claim_publication(
            review_tuple,
            lease_token=lease_token,
            now=121,
            lease_seconds=300,
        )
    replacement = second_store.reserve(review_tuple, now=420, lease_seconds=30)
    assert isinstance(replacement, str)


def test_settlement_control_plane_claims_publication_before_side_effect(monkeypatch, tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    review_tuple = ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )
    lease_token = store.reserve(review_tuple, now=100, lease_seconds=60)
    assert isinstance(lease_token, str)
    monkeypatch.setattr(gate.time, "time", lambda: 120)
    payload = {
        "operation": "claim_publish",
        "contract_version": "v2",
        "repository": review_tuple.repository,
        "pr_number": review_tuple.pr_number,
        "base_sha": review_tuple.base_sha,
        "head_sha": review_tuple.head_sha,
        "lease_token": lease_token,
    }

    assert gate._settle(payload, "newtonsapple-bot", store) == {
        "settled": "claim_publish"
    }
    with pytest.raises(ValueError, match="review publication claim not found"):
        gate._settle(payload, "newtonsapple-bot", store)


def test_review_failures_back_off_and_eventually_dead_letter(tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    review_tuple = ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )

    first_token = store.reserve(review_tuple, now=100, lease_seconds=30)
    assert isinstance(first_token, str)
    first = store.record_failure(
        review_tuple,
        lease_token=first_token,
        now=110,
        retry_delay=60,
        max_attempts=2,
        dead_letter_marker="dead-letter-marker",
        dead_letter_content="review failed permanently",
    )
    assert first == {"attempts": 1, "dead_lettered": False, "retry_after": 170}
    assert store.reserve(review_tuple, now=169, lease_seconds=30) is None
    second_token = store.reserve(review_tuple, now=170, lease_seconds=30)
    assert isinstance(second_token, str)

    second = store.record_failure(
        review_tuple,
        lease_token=second_token,
        now=180,
        retry_delay=60,
        max_attempts=2,
        dead_letter_marker="dead-letter-marker",
        dead_letter_content="review failed permanently",
    )
    assert second == {"attempts": 2, "dead_lettered": True, "retry_after": None}
    assert store.pending_summaries() == [
        {
            "id": 1,
            "key": f"blocker:dead-letter:{gate.tuple_key(review_tuple)}",
            "marker": "dead-letter-marker",
            "content": "review failed permanently",
        }
    ]
    assert store.reserve(review_tuple, now=10_000, lease_seconds=30) is None


def test_webhook_gate_returns_the_opaque_lease_token(monkeypatch, tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    live_pr = _live_pr()
    monkeypatch.setattr(
        "scripts.newtonsapple_pr_review_gate._live_review_state",
        lambda number, login: (live_pr, [], []),
    )

    result = _gate_webhook(
        {
            "action": "review_requested",
            "number": 185,
            "repository": {"full_name": "NewtonsAppleAI/newtonsapple-web"},
            "requested_reviewer": {"login": "newtonsapple-bot"},
            "pull_request": live_pr,
        },
        "newtonsapple-bot",
        store,
    )

    assert result["contract_version"] == "v2"
    assert result["expected_base_ref"] == "dev"
    assert isinstance(result["lease_token"], str)
    assert len(result["lease_token"]) >= 32


def test_summary_outbox_is_tuple_unique_and_replay_checks_buzz_before_sending(tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    review_tuple = ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )
    marker = (
        "<!-- newtonsapple-pr-review-summary:v2 "
        f"repo=NewtonsAppleAI/newtonsapple-web pr=185 base={BASE_SHA} head={HEAD_SHA} -->"
    )

    first_id = store.enqueue_summary(review_tuple, marker=marker, content="review summary")
    second_id = store.enqueue_summary(review_tuple, marker=marker, content="replacement ignored")

    assert first_id == second_id
    assert store.pending_summaries() == [
        {
            "id": first_id,
            "key": (
                f"v2:NewtonsAppleAI/newtonsapple-web:185:{BASE_SHA}:{HEAD_SHA}"
            ),
            "marker": marker,
            "content": "review summary",
        }
    ]

    sent = []
    processed = drain_summary_outbox(
        store,
        find_existing=lambda candidate: "buzz-event-existing"
        if candidate == marker
        else None,
        send=lambda content: sent.append(content) or "buzz-event-new",
    )

    assert processed == 1
    assert sent == []
    assert store.pending_summaries() == []
    assert store.sent_event_id(first_id) == "buzz-event-existing"


def test_buzz_marker_reconciliation_requires_configured_channel_and_own_author(monkeypatch):
    marker = "<!-- tuple-marker -->"
    own_pubkey = "b" * 64
    responses = iter(
        [
            [{"display_name": "Hermany", "pubkey": own_pubkey}],
            [
                {
                    "id": "wrong-channel",
                    "pubkey": own_pubkey,
                    "content": marker,
                    "tags": [["h", "different-channel"]],
                },
                {
                    "id": "forged-same-channel",
                    "pubkey": "a" * 64,
                    "content": marker,
                    "tags": [["h", "b1cb95c9-6a36-4516-abdd-81d853a9412e"]],
                },
                {
                    "id": "quoted-by-own-author",
                    "pubkey": own_pubkey,
                    "content": f"quoted {marker} but not a settled outbox message",
                    "tags": [["h", "b1cb95c9-6a36-4516-abdd-81d853a9412e"]],
                },
                {
                    "id": "expected-channel-and-author",
                    "pubkey": own_pubkey,
                    "content": marker,
                    "tags": [["h", "b1cb95c9-6a36-4516-abdd-81d853a9412e"]],
                },
            ],
        ]
    )
    monkeypatch.setattr(
        "scripts.newtonsapple_pr_review_gate.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(next(responses)),
        ),
    )

    assert _buzz_find(marker) == "expected-channel-and-author"


def test_summary_outbox_remains_pending_when_buzz_delivery_fails(tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    review_tuple = ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )
    store.enqueue_summary(review_tuple, marker="tuple-marker", content="summary")

    processed = drain_summary_outbox(
        store,
        find_existing=lambda marker: None,
        send=lambda content: (_ for _ in ()).throw(RuntimeError("Buzz offline")),
    )

    assert processed == 0
    assert len(store.pending_summaries()) == 1


def test_summary_outbox_claims_are_exclusive_and_stale_tokens_cannot_ack(tmp_path):
    path = tmp_path / "review.sqlite3"
    first_store = ReviewStateStore(path)
    second_store = ReviewStateStore(path)
    review_tuple = ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )
    outbox_id = first_store.enqueue_summary(
        review_tuple, marker="tuple-marker", content="summary"
    )

    first_claim = first_store.claim_summary(now=100, lease_seconds=30)
    assert first_claim is not None
    assert second_store.claim_summary(now=129, lease_seconds=30) is None
    second_claim = second_store.claim_summary(now=130, lease_seconds=30)
    assert second_claim is not None
    assert second_claim["claim_token"] != first_claim["claim_token"]

    with pytest.raises(ValueError, match="outbox claim not found"):
        first_store.mark_summary_sent(
            outbox_id, "stale-event", claim_token=first_claim["claim_token"]
        )
    second_store.mark_summary_sent(
        outbox_id, "accepted-event", claim_token=second_claim["claim_token"]
    )
    assert first_store.sent_event_id(outbox_id) == "accepted-event"


def test_buzz_outbox_claim_can_be_token_fenced_and_renewed(tmp_path):
    first_store = ReviewStateStore(tmp_path / "review.sqlite3")
    second_store = ReviewStateStore(tmp_path / "review.sqlite3")
    review_tuple = ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )
    outbox_id = first_store.enqueue_summary(
        review_tuple, marker="tuple-marker", content="summary"
    )
    claim = first_store.claim_summary(now=100, lease_seconds=60)
    assert claim is not None

    first_store.renew_summary_claim(
        outbox_id,
        claim_token=claim["claim_token"],
        now=150,
        lease_seconds=300,
    )

    assert second_store.claim_summary(now=200, lease_seconds=60) is None
    with pytest.raises(ValueError, match="outbox claim not found"):
        first_store.renew_summary_claim(
            outbox_id,
            claim_token="stale-token",
            now=200,
            lease_seconds=300,
        )


def test_operational_blocker_has_distinct_outbox_identity_from_later_summary(tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    review_tuple = ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )

    blocker_id = store.enqueue_blocker(
        review_tuple, marker="blocker-marker", content="missing trusted capture"
    )
    summary_id = store.enqueue_summary(
        review_tuple, marker="summary-marker", content="review completed"
    )

    assert blocker_id != summary_id
    assert [item["content"] for item in store.pending_summaries()] == [
        "missing trusted capture",
        "review completed",
    ]


def _live_pr(**overrides):
    pull = {
        "number": 185,
        "state": "open",
        "draft": False,
        "html_url": "https://github.com/NewtonsAppleAI/newtonsapple-web/pull/185",
        "base": {"ref": "dev", "sha": BASE_SHA},
        "head": {"ref": "chore--review", "sha": HEAD_SHA},
        "requested_reviewers": [{"login": "newtonsapple-bot"}],
    }
    pull.update(overrides)
    return pull


def test_reconciliation_selects_only_live_exact_tuple_with_no_bot_marker():
    trusted = TrustedWorkflow(
        workflow_id=778899,
        path=".github/workflows/pr-review-capture.yml",
        branch="dev",
    )
    run = _capture_run()

    selected = select_authorized_tuple(
        _live_pr(),
        statuses=[_pending_status()],
        load_run=lambda run_id: run if run_id == run["id"] else None,
        trusted_workflow=trusted,
        reviewer_login="newtonsapple-bot",
        bot_bodies=[],
    )

    assert selected == ReviewTuple(
        repository="NewtonsAppleAI/newtonsapple-web",
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )


def test_reconcile_does_not_settle_an_untrusted_legacy_bot_marker(monkeypatch, tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    live_pr = _live_pr()
    review_tuple = ReviewTuple(
        repository=gate.REPOSITORY,
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )
    marker_body = f"legacy review\n\n{gate.review_marker(review_tuple)}"
    state = (live_pr, None, [marker_body])
    monkeypatch.setattr(gate, "_collection", lambda endpoint: [live_pr])
    monkeypatch.setattr(gate, "_authorized_live_tuple", lambda *args: state)
    monkeypatch.setattr(
        gate, "_captured_live_tuple", lambda *args: state, raising=False
    )
    terminal_statuses = []
    monkeypatch.setattr(
        gate,
        "_post_terminal_status",
        lambda candidate, url: terminal_statuses.append((candidate, url)),
    )
    monkeypatch.setattr(gate, "_buzz_find", lambda marker: None)
    monkeypatch.setattr(gate, "_buzz_send", lambda content: "buzz-blocker")

    result = gate._reconcile("newtonsapple-bot", store)

    assert result["events"] == []
    assert terminal_statuses == []


def test_reconcile_settles_existing_marker_only_with_trusted_capture(monkeypatch, tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    live_pr = _live_pr()
    review_tuple = ReviewTuple(
        repository=gate.REPOSITORY,
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )
    marker_body = f"verified review\n\n{gate.review_marker(review_tuple)}"
    monkeypatch.setattr(gate, "_collection", lambda endpoint: [live_pr])
    monkeypatch.setattr(
        gate,
        "_captured_live_tuple",
        lambda *args: (live_pr, review_tuple, [marker_body]),
    )
    terminal_statuses = []
    monkeypatch.setattr(
        gate,
        "_post_terminal_status",
        lambda candidate, url: terminal_statuses.append((candidate, url)),
    )
    monkeypatch.setattr(gate, "_buzz_find", lambda marker: None)
    monkeypatch.setattr(gate, "_buzz_send", lambda content: "buzz-summary")

    result = gate._reconcile("newtonsapple-bot", store)

    assert result == {"events": [], "outbox_delivered": 1}
    assert terminal_statuses == [(review_tuple, live_pr["html_url"])]
    assert store.reserve(review_tuple, now=200, lease_seconds=30) is None


def test_reconcile_isolates_a_malformed_candidate_and_continues(monkeypatch, tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    first = _live_pr()
    second_head = "c" * 40
    second = _live_pr(
        number=186,
        html_url="https://github.com/NewtonsAppleAI/newtonsapple-web/pull/186",
        head={"ref": "feature", "sha": second_head},
    )
    second_tuple = ReviewTuple(
        repository=gate.REPOSITORY,
        pr_number=186,
        base_sha=BASE_SHA,
        head_sha=second_head,
    )
    monkeypatch.setattr(gate, "_collection", lambda endpoint: [first, second])

    def captured(number, login):
        if number == 185:
            raise RuntimeError("malformed status provenance")
        return second, second_tuple, []

    monkeypatch.setattr(gate, "_captured_live_tuple", captured)
    delivered = []
    monkeypatch.setattr(gate, "_buzz_find", lambda marker: None)
    monkeypatch.setattr(
        gate,
        "_buzz_send",
        lambda content: delivered.append(content) or "buzz-blocker",
    )

    result = gate._reconcile("newtonsapple-bot", store)

    assert len(result["events"]) == 1
    assert result["events"][0]["payload"]["number"] == 186
    assert result["outbox_delivered"] == 1
    assert "could not safely verify current request provenance" in delivered[0]


def test_reconciliation_accepts_latest_current_request_when_capture_status_is_missing():
    timeline = [
        {"id": 1, "event": "committed"},
        {
            "id": 2,
            "event": "review_requested",
            "requested_reviewer": {"login": "newtonsapple-bot"},
        },
    ]

    selected = select_authorized_tuple(
        _live_pr(),
        statuses=[],
        load_run=lambda run_id: None,
        trusted_workflow=TrustedWorkflow(
            workflow_id=778899,
            path=".github/workflows/pr-review-capture.yml",
            branch="dev",
        ),
        reviewer_login="newtonsapple-bot",
        bot_bodies=[],
        load_timeline=lambda pr_number: timeline,
    )

    assert selected == ReviewTuple(
        repository=gate.REPOSITORY,
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )


@pytest.mark.parametrize(
    "later_event",
    [
        {"id": 3, "event": "committed"},
        {"id": 3, "event": "head_ref_force_pushed"},
        {"id": 3, "event": "base_ref_changed"},
        {
            "id": 3,
            "event": "review_request_removed",
            "requested_reviewer": {"login": "newtonsapple-bot"},
        },
        {"id": 3, "event": "converted_to_draft"},
        {"id": 3, "event": "closed"},
        {"id": 3, "event": "merged"},
    ],
)
def test_reconciliation_without_capture_rejects_events_after_latest_request(later_event):
    timeline = [
        {
            "id": 2,
            "event": "review_requested",
            "requested_reviewer": {"login": "newtonsapple-bot"},
        },
        later_event,
    ]

    selected = select_authorized_tuple(
        _live_pr(),
        statuses=[],
        load_run=lambda run_id: None,
        trusted_workflow=TrustedWorkflow(
            workflow_id=778899,
            path=".github/workflows/pr-review-capture.yml",
            branch="dev",
        ),
        reviewer_login="newtonsapple-bot",
        bot_bodies=[],
        load_timeline=lambda pr_number: timeline,
    )

    assert selected is None


def test_reconciliation_without_capture_accepts_the_latest_rerequest():
    timeline = [
        {
            "id": 2,
            "event": "review_requested",
            "requested_reviewer": {"login": "newtonsapple-bot"},
        },
        {
            "id": 3,
            "event": "review_request_removed",
            "requested_reviewer": {"login": "newtonsapple-bot"},
        },
        {
            "id": 4,
            "event": "review_requested",
            "requested_reviewer": {"login": "newtonsapple-bot"},
        },
    ]

    selected = select_authorized_tuple(
        _live_pr(),
        statuses=[],
        load_run=lambda run_id: None,
        trusted_workflow=TrustedWorkflow(
            workflow_id=778899,
            path=".github/workflows/pr-review-capture.yml",
            branch="dev",
        ),
        reviewer_login="newtonsapple-bot",
        bot_bodies=[],
        load_timeline=lambda pr_number: timeline,
    )

    assert selected == ReviewTuple(
        repository=gate.REPOSITORY,
        pr_number=185,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )


def test_webhook_accepts_verified_payload_tuple_without_capture_status(monkeypatch, tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    live_pr = _live_pr()
    monkeypatch.setattr(
        gate,
        "_live_review_state",
        lambda number, login: (live_pr, [], []),
    )

    result = _gate_webhook(
        {
            "action": "review_requested",
            "number": 185,
            "repository": {"full_name": "NewtonsAppleAI/newtonsapple-web"},
            "requested_reviewer": {"login": "newtonsapple-bot"},
            "pull_request": live_pr,
        },
        "newtonsapple-bot",
        store,
    )

    assert result["expected_base_sha"] == BASE_SHA
    assert result["expected_head_sha"] == HEAD_SHA


def test_webhook_rejects_head_mutation_between_payload_and_live_state(monkeypatch, tmp_path):
    store = ReviewStateStore(tmp_path / "review.sqlite3")
    payload_pr = _live_pr()
    live_pr = _live_pr(head={"ref": "chore--review", "sha": "c" * 40})
    monkeypatch.setattr(
        gate,
        "_live_review_state",
        lambda number, login: (live_pr, [], []),
    )

    with pytest.raises(RuntimeError, match="tuple changed"):
        _gate_webhook(
            {
                "action": "review_requested",
                "number": 185,
                "repository": {"full_name": "NewtonsAppleAI/newtonsapple-web"},
                "requested_reviewer": {"login": "newtonsapple-bot"},
                "pull_request": payload_pr,
            },
            "newtonsapple-bot",
            store,
        )


def test_local_execution_worker_attempts_every_gate_without_credentials(monkeypatch):
    monkeypatch.setattr(
        gate,
        "_commit_tree_sha",
        lambda sha: "c" * 40 if sha == BASE_SHA else "d" * 40,
    )
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-propagate")
    monkeypatch.setenv("GH_TOKEN", "must-not-propagate")
    monkeypatch.setattr(gate, "_local_docker_host", lambda: "unix:///tmp/docker.sock")
    fetched = []
    monkeypatch.setattr(
        gate,
        "_fetch_exact_commit",
        lambda workspace, sha, *, home: fetched.append((sha, home)),
    )
    monkeypatch.setattr(
        gate,
        "_git_output",
        lambda workspace, *args: (
            HEAD_SHA
            if args == ("rev-parse", "HEAD")
            else "d" * 40
            if args == ("rev-parse", "HEAD^{tree}")
            else "commit"
            if args == ("cat-file", "-t", BASE_SHA)
            else ""
        ),
    )
    calls = []

    def fake_run(command, *, cwd, env, timeout):
        calls.append((command, env))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(gate, "_run_command", fake_run)

    result = gate._run_local_execution_worker(
        ReviewTuple(
            repository=gate.REPOSITORY,
            pr_number=185,
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
        )
    )

    assert result["worker"]["required"] is True
    assert result["worker"]["preflight"]["host_mounts_absent"] is True
    assert [item["status"] for item in result["gates"]] == ["pass", "pass", "pass"]
    assert all(item["attempted"] is True for item in result["gates"])
    assert [sha for sha, _ in fetched] == [HEAD_SHA, BASE_SHA]
    docker_calls = [(command, env) for command, env in calls if command[0] == "docker"]
    assert len(docker_calls) == 6
    assert [command[command.index("--network") + 1] for command, _ in docker_calls] == [
        "bridge",
        "none",
        "bridge",
        "none",
        "bridge",
        "none",
    ]
    assert all(env["DOCKER_HOST"] == "unix:///tmp/docker.sock" for _, env in docker_calls)
    assert all("GITHUB_TOKEN" not in env and "GH_TOKEN" not in env for _, env in docker_calls)


def test_local_execution_worker_reports_each_gate_when_docker_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        gate,
        "_commit_tree_sha",
        lambda sha: "c" * 40 if sha == BASE_SHA else "d" * 40,
    )
    monkeypatch.setattr(gate, "_local_docker_host", lambda: "unix:///tmp/docker.sock")
    monkeypatch.setattr(gate, "_fetch_exact_commit", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gate,
        "_git_output",
        lambda workspace, *args: (
            HEAD_SHA
            if args == ("rev-parse", "HEAD")
            else "d" * 40
            if args == ("rev-parse", "HEAD^{tree}")
            else "commit"
            if args == ("cat-file", "-t", BASE_SHA)
            else ""
        ),
    )
    docker_calls = []

    def fake_run(command, *, cwd, env, timeout):
        if command[0] == "docker":
            docker_calls.append(command)
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="failed to connect to the docker API",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gate, "_run_command", fake_run)

    result = gate._run_local_execution_worker(
        ReviewTuple(
            repository=gate.REPOSITORY,
            pr_number=185,
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
        )
    )

    assert len(docker_calls) == 6
    assert [item["status"] for item in result["gates"]] == [
        "unavailable",
        "unavailable",
        "unavailable",
    ]
    assert all(item["attempted"] is False for item in result["gates"])


def test_local_execution_reports_unavailable_gates_in_signed_evidence(monkeypatch):
    private_key = _install_attestation_key(monkeypatch)
    monkeypatch.setattr(
        gate,
        "_run_local_execution_worker",
        lambda review_tuple: {
            "base_tree_sha": "c" * 40,
            "head_tree_sha": "d" * 40,
            "worker": {"required": True, "isolation": "docker"},
            "gates": [
                gate._unavailable_local_gate(name, "local worker unavailable")
                for name in gate.BASELINE_EXECUTION_GATES
            ],
        },
    )
    monkeypatch.setattr(
        gate,
        "_trusted_ci_evidence",
        lambda review_tuple: (_ for _ in ()).throw(RuntimeError("Actions unavailable")),
    )

    result = gate.execution_evidence(_execution_request("execution_evidence"))

    payload = base64.b64decode(result["attestation_payload"])
    private_key.public_key().verify(
        base64.b64decode(result["attestation_signature"]), payload
    )
    report = json.loads(payload)
    assert [item["status"] for item in report["gates"]] == [
        "unavailable",
        "unavailable",
        "unavailable",
    ]


def test_exact_commit_fetch_uses_only_the_pinned_gh_credential_helper(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("GH_CONFIG_DIR", "/trusted/gh-config")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-propagate")
    calls = []

    def fake_run(command, *, cwd, env, timeout):
        calls.append((command, cwd, env, timeout))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gate, "_run_command", fake_run)

    gate._fetch_exact_commit(workspace, HEAD_SHA, home=home)

    assert calls == [
        (
            [
                "git",
                "-c",
                "credential.helper=",
                "-c",
                "credential.helper=!gh auth git-credential",
                "fetch",
                "-q",
                "--depth=1",
                "origin",
                HEAD_SHA,
            ],
            workspace,
            {
                "PATH": gate.os.environ.get(
                    "PATH", "/usr/bin:/bin:/usr/sbin:/sbin"
                ),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "HOME": str(home),
                "GH_CONFIG_DIR": "/trusted/gh-config",
            },
            180,
        )
    ]


def test_local_docker_host_resolves_only_a_unix_socket(monkeypatch):
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="unix:///Users/reviewer/.docker/run/docker.sock\n",
            stderr="",
        ),
    )

    assert gate._local_docker_host() == (
        "unix:///Users/reviewer/.docker/run/docker.sock"
    )


def test_local_docker_host_rejects_remote_daemon(monkeypatch):
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="tcp://docker.example:2375\n",
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="local Docker socket"):
        gate._local_docker_host()


def test_gate_resolution_falls_back_to_status_agnostic_local_contracts(monkeypatch):
    private_key = _install_attestation_key(monkeypatch)
    monkeypatch.setattr(
        gate,
        "_trusted_ci_evidence",
        lambda _review_tuple: (_ for _ in ()).throw(
            RuntimeError("Actions unavailable")
        ),
    )

    result = gate.resolve_execution_gates(_execution_request("resolve_execution_gates"))

    payload = base64.b64decode(result["gate_resolution_payload"])
    signature = base64.b64decode(result["gate_resolution_signature"])
    private_key.public_key().verify(signature, payload)
    signed = json.loads(payload)
    assert {
        name: contract["statuses"]
        for name, contract in signed["gate_contracts"].items()
    } == {
        "quality": ["pass", "pr-fail", "unavailable"],
        "integration": ["pass", "pr-fail", "unavailable"],
        "e2e": ["pass", "pr-fail", "unavailable"],
    }


def test_dispatch_reconciliation_rechecks_request_event_and_later_invalidation():
    trusted = TrustedWorkflow(
        workflow_id=778899,
        path=".github/workflows/pr-review-capture.yml",
        branch="dev",
        allowed_dispatchers=("bas4r",),
    )
    run = _dispatch_run()
    timeline = [
        {
            "id": 29064129383,
            "event": "review_requested",
            "requested_reviewer": {"login": "newtonsapple-bot"},
        }
    ]

    selected = select_authorized_tuple(
        _live_pr(),
        statuses=[_pending_status()],
        load_run=lambda run_id: run,
        load_timeline=lambda pr_number: timeline,
        trusted_workflow=trusted,
        reviewer_login="newtonsapple-bot",
        bot_bodies=[],
    )
    assert selected is not None

    timeline.append({"id": 29064129384, "event": "committed"})
    rejected = select_authorized_tuple(
        _live_pr(),
        statuses=[_pending_status()],
        load_run=lambda run_id: run,
        load_timeline=lambda pr_number: timeline,
        trusted_workflow=trusted,
        reviewer_login="newtonsapple-bot",
        bot_bodies=[],
    )
    assert rejected is None


@pytest.mark.parametrize(
    ("live_overrides", "bot_bodies"),
    [
        ({"state": "closed"}, []),
        ({"draft": True}, []),
        ({"base": {"ref": "feature", "sha": BASE_SHA}}, []),
        ({"base": {"ref": "dev", "sha": "c" * 40}}, []),
        ({"head": {"ref": "chore--review", "sha": "c" * 40}}, []),
        ({"requested_reviewers": []}, []),
        ({}, ["prefix <!-- newtonsapple-pr-review:v2 repo=NewtonsAppleAI/newtonsapple-web "
              f"pr=185 base={BASE_SHA} head={HEAD_SHA} --> suffix"]),
    ],
)
def test_reconciliation_rejects_stale_ineligible_or_completed_tuple(
    live_overrides, bot_bodies
):
    trusted = TrustedWorkflow(
        workflow_id=778899,
        path=".github/workflows/pr-review-capture.yml",
        branch="dev",
    )

    selected = select_authorized_tuple(
        _live_pr(**live_overrides),
        statuses=[_pending_status()],
        load_run=lambda run_id: _capture_run(),
        trusted_workflow=trusted,
        reviewer_login="newtonsapple-bot",
        bot_bodies=bot_bodies,
    )

    assert selected is None
