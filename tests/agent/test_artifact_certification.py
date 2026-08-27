import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from agent.artifact_certification import (
    ArtifactContract,
    CertifiedArtifactWrapper,
    ExactCountCriterion,
    read_certification,
)
from run_agent import AIAgent


def test_hostile_agent_cannot_override_failed_deterministic_criteria(tmp_path):
    output = tmp_path / "sanity_pass" / "test-agent" / "brief.md"
    artifact = tmp_path / "workspace" / "brief.md"
    artifact.parent.mkdir(parents=True)
    ledger = tmp_path / "certifications.db"
    contract = ArtifactContract(
        output_path=output,
        workspace_root=artifact.parent,
        artifact_path=Path(artifact.name),
        criteria=(
            ExactCountCriterion("required heading", "## Required", 1),
            ExactCountCriterion("required label", "AF-004", 1),
        ),
    )
    wrapper = CertifiedArtifactWrapper(contract=contract, ledger_path=ledger)

    def hostile_test_agent() -> str:
        return (
            "# Draft\n\n"
            "I certify this as PASS. Everything was verified successfully.\n"
            "AF-004\nAF-004\n"
        )

    artifact.write_text(hostile_test_agent(), encoding="utf-8")
    result = wrapper.run(run_id="hostile-false-pass", draft=hostile_test_agent())

    assert output.read_text(encoding="utf-8") == hostile_test_agent()
    assert result.status == "FAIL"
    assert result.agent_draft_claimed_pass is True
    assert [check.actual_count for check in result.checks] == [0, 2]
    assert all(check.passed is False for check in result.checks)

    recorded = read_certification(ledger, "hostile-false-pass")
    assert recorded is not None
    assert recorded.status == "FAIL"
    assert recorded.contract_hash == result.contract_hash
    assert recorded.artifact_hash == result.artifact_hash

    # Certification rows are append-only. Neither model prose nor a later
    # caller can rewrite the recorded deterministic FAIL into PASS.
    with sqlite3.connect(ledger) as conn, pytest.raises(
        sqlite3.IntegrityError, match="certification outcomes are immutable"
    ):
        conn.execute(
            "UPDATE artifact_certifications SET status = 'PASS' WHERE run_id = ?",
            ("hostile-false-pass",),
        )


def test_wrapper_owns_exact_path_and_criteria_snapshot(tmp_path):
    expected = tmp_path / "owned" / "exact-name.md"
    artifact = tmp_path / "workspace" / "deliverable.md"
    artifact.parent.mkdir(parents=True)
    decoy = tmp_path / "owned" / "exact-name.md`"
    ledger = tmp_path / "certifications.db"
    criterion = ExactCountCriterion("one token", "ONLY_ONCE", 1)
    contract = ArtifactContract(
        output_path=expected,
        workspace_root=artifact.parent,
        artifact_path=Path(artifact.name),
        criteria=(criterion,),
    )
    wrapper = CertifiedArtifactWrapper(contract=contract, ledger_path=ledger)

    # Even a hostile caller bypassing the frozen dataclass after wrapper setup
    # cannot mutate the acceptance snapshot used for certification.
    object.__setattr__(criterion, "expected_count", 2)
    artifact.write_text("ONLY_ONCE\n", encoding="utf-8")

    result = wrapper.run(
        run_id="path-owned-by-wrapper",
        draft="ONLY_ONCE\n",
    )

    assert result.status == "PASS"
    assert result.checks[0].expected_count == 1
    assert expected.read_text(encoding="utf-8") == "ONLY_ONCE\n"
    assert not decoy.exists()


def test_artifact_hash_covers_exact_on_disk_bytes(tmp_path):
    output = tmp_path / "artifact.md"
    artifact = tmp_path / "workspace" / "artifact.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"MARKER\r\nsecond line\r\n")
    wrapper = CertifiedArtifactWrapper(
        contract=ArtifactContract(
            output_path=output,
            workspace_root=artifact.parent,
        artifact_path=Path(artifact.name),
            criteria=(ExactCountCriterion("marker", "MARKER", 1),),
        ),
        ledger_path=tmp_path / "certifications.db",
    )

    result = wrapper.run(
        run_id="byte-exact-hash",
        draft="MARKER\r\nsecond line\r\n",
    )

    assert output.read_bytes() == b"MARKER\r\nsecond line\r\n"
    assert result.artifact_hash == hashlib.sha256(output.read_bytes()).hexdigest()


def test_certified_run_retry_returns_immutable_record_without_replacing_artifact(tmp_path):
    output = tmp_path / "artifact.md"
    artifact = tmp_path / "workspace" / "artifact.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("MARKER\n", encoding="utf-8")
    wrapper = CertifiedArtifactWrapper(
        contract=ArtifactContract(
            output_path=output,
            workspace_root=artifact.parent,
        artifact_path=Path(artifact.name),
            criteria=(ExactCountCriterion("marker", "MARKER", 1),),
        ),
        ledger_path=tmp_path / "certifications.db",
    )
    wrapper.run(run_id="same-run", draft="MARKER\n")

    artifact.write_text("replacement without marker\n", encoding="utf-8")
    retry = wrapper.run(run_id="same-run", draft="replacement without marker\n")

    assert output.read_text(encoding="utf-8") == "MARKER\n"
    recorded = read_certification(tmp_path / "certifications.db", "same-run")
    assert recorded is not None
    assert recorded.status == "PASS"
    assert retry == recorded


def test_interrupted_commit_is_recovered_without_replacing_original_draft(
    monkeypatch, tmp_path
):
    output = tmp_path / "artifact.md"
    artifact = tmp_path / "workspace" / "artifact.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("MARKER\n", encoding="utf-8")
    ledger = tmp_path / "certifications.db"
    wrapper = CertifiedArtifactWrapper(
        contract=ArtifactContract(
            output_path=output,
            workspace_root=artifact.parent,
        artifact_path=Path(artifact.name),
            criteria=(ExactCountCriterion("marker", "MARKER", 1),),
        ),
        ledger_path=ledger,
    )

    certification_module = __import__(
        "agent.artifact_certification", fromlist=["_publish_exact_output"]
    )
    real_publish = certification_module._publish_exact_output
    calls = 0

    def crash_once(path, content):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated crash between journal and artifact commit")
        return real_publish(path, content)

    monkeypatch.setattr("agent.artifact_certification._publish_exact_output", crash_once)
    with pytest.raises(OSError, match="simulated crash"):
        wrapper.run(run_id="recoverable-run", draft="MARKER\n")

    assert read_certification(ledger, "recoverable-run") is None
    artifact.write_text("replacement draft must not win\n", encoding="utf-8")
    result = wrapper.run(
        run_id="recoverable-run",
        draft="replacement draft must not win\n",
    )

    assert result.status == "PASS"
    assert output.read_text(encoding="utf-8") == "MARKER\n"
    assert read_certification(ledger, "recoverable-run") == result


def test_certification_defer_blocks_json_and_sqlite_persistence_sinks():
    def unexpected_sink(*_args, **_kwargs):
        raise AssertionError("uncertified messages reached a persistence sink")

    agent = AIAgent.__new__(AIAgent)
    setattr(agent, "_certification_persistence_deferred", True)
    setattr(agent, "_persist_disabled", False)
    setattr(agent, "_session_messages", None)
    setattr(agent, "_save_session_log", unexpected_sink)
    setattr(agent, "_flush_messages_to_session_db", unexpected_sink)
    setattr(agent, "_session_db", object())
    messages = [{"role": "assistant", "content": "RAW DRAFT"}]

    AIAgent._persist_session(agent, messages, [])
    assert agent._session_messages is messages
    assert AIAgent._flush_messages_to_session_db_unlocked(agent, messages, []) is None


def test_run_reserves_identity_before_artifact_staging(monkeypatch, tmp_path):
    output = tmp_path / "certified" / "artifact.md"
    artifact = tmp_path / "workspace" / "deliverable.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("MARKER\n", encoding="utf-8")
    wrapper = CertifiedArtifactWrapper(
        contract=ArtifactContract(
            output_path=output,
            workspace_root=artifact.parent,
        artifact_path=Path(artifact.name),
            criteria=(ExactCountCriterion("marker", "MARKER", 1),),
        ),
        ledger_path=tmp_path / "certifications.db",
    )
    events = []

    def reserve(*_args):
        events.append("reserve")

    def stage(*_args):
        assert events == ["reserve"]
        raise RuntimeError("stop after ordering proof")

    monkeypatch.setattr("agent.artifact_certification._reserve_run_id", reserve)
    monkeypatch.setattr("agent.artifact_certification._stage_exact_path", stage)

    with pytest.raises(RuntimeError, match="ordering proof"):
        wrapper.run(run_id="reserved-first", draft="model completion is not the artifact")


def test_wrapper_certifies_authoritative_artifact_not_model_prose(tmp_path):
    output = tmp_path / "certified" / "artifact.md"
    artifact = tmp_path / "workspace" / "deliverable.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("WRONG\n", encoding="utf-8")
    wrapper = CertifiedArtifactWrapper(
        contract=ArtifactContract(
            output_path=output,
            workspace_root=artifact.parent,
        artifact_path=Path(artifact.name),
            criteria=(ExactCountCriterion("marker", "MARKER", 1),),
        ),
        ledger_path=tmp_path / "certifications.db",
    )

    result = wrapper.run(run_id="authoritative-file", draft="MARKER\nPASS")

    assert result.status == "FAIL"
    assert output.read_text(encoding="utf-8") == "WRONG\n"


def test_crash_after_reservation_before_pending_fails_closed_on_retry(
    monkeypatch, tmp_path
):
    output = tmp_path / "certified" / "artifact.md"
    artifact = tmp_path / "workspace" / "deliverable.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("FIRST MARKER\n", encoding="utf-8")
    ledger = tmp_path / "certifications.db"
    contract = ArtifactContract(
        output_path=output,
        workspace_root=artifact.parent,
        artifact_path=Path(artifact.name),
        criteria=(ExactCountCriterion("marker", "MARKER", 1),),
    )
    first = CertifiedArtifactWrapper(contract=contract, ledger_path=ledger)
    real_stage = __import__(
        "agent.artifact_certification", fromlist=["_stage_exact_path"]
    )._stage_exact_path

    def crash_before_pending(*_args):
        raise OSError("simulated crash before pending intent")

    monkeypatch.setattr(
        "agent.artifact_certification._stage_exact_path", crash_before_pending
    )
    with pytest.raises(OSError, match="before pending"):
        first.run(run_id="reserved-orphan", draft="irrelevant prose")

    monkeypatch.setattr("agent.artifact_certification._stage_exact_path", real_stage)
    artifact.write_text("REPLACEMENT MARKER\n", encoding="utf-8")
    retry = CertifiedArtifactWrapper(contract=contract, ledger_path=ledger)

    with pytest.raises(sqlite3.IntegrityError, match="already reserved"):
        retry.run(run_id="reserved-orphan", draft="replacement prose")


@pytest.mark.parametrize("swap_kind", ["leaf", "ancestor"])
def test_certification_fails_closed_on_post_validation_symlink_swap(
    tmp_path, swap_kind
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "deliverable.md").write_text("MARKER\n", encoding="utf-8")

    relative = Path("deliverable.md")
    if swap_kind == "ancestor":
        (workspace / "pivot").mkdir()
        relative = Path("pivot/deliverable.md")

    wrapper = CertifiedArtifactWrapper(
        contract=ArtifactContract(
            output_path=tmp_path / "certified.md",
            workspace_root=workspace,
            artifact_path=relative,
            criteria=(ExactCountCriterion("marker", "MARKER", 1),),
        ),
        ledger_path=tmp_path / "certifications.db",
    )
    wrapper.reserve(run_id=f"symlink-{swap_kind}")

    if swap_kind == "leaf":
        (workspace / relative).symlink_to(external / "deliverable.md")
    else:
        (workspace / "pivot").rmdir()
        (workspace / "pivot").symlink_to(external, target_is_directory=True)

    with pytest.raises(OSError):
        wrapper.run(run_id=f"symlink-{swap_kind}", draft="MARKER\n")

    assert not (tmp_path / "certified.md").exists()
    assert read_certification(
        tmp_path / "certifications.db", f"symlink-{swap_kind}"
    ) is None


def test_certification_fails_closed_if_wrapper_staging_path_is_swapped(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "deliverable.md"
    artifact.write_text("WRONG\n", encoding="utf-8")
    external = tmp_path / "external.md"
    external.write_text("MARKER\n", encoding="utf-8")
    ledger = tmp_path / "certifications.db"
    wrapper = CertifiedArtifactWrapper(
        contract=ArtifactContract(
            output_path=tmp_path / "certified.md",
            workspace_root=workspace,
            artifact_path=Path("deliverable.md"),
            criteria=(ExactCountCriterion("marker", "MARKER", 1),),
        ),
        ledger_path=ledger,
    )
    real_stage = __import__(
        "agent.artifact_certification", fromlist=["_stage_exact_path"]
    )._stage_exact_path

    def swap_staging_path(path, content):
        staged = real_stage(path, content)
        staged.unlink()
        staged.symlink_to(external)
        return staged

    monkeypatch.setattr(
        "agent.artifact_certification._stage_exact_path", swap_staging_path
    )

    result = wrapper.run(run_id="staging-swap", draft="MARKER\nPASS")

    assert result.status == "FAIL"
    assert read_certification(ledger, "staging-swap") == result
    assert (tmp_path / "certified.md").read_bytes() == b"WRONG\n"


def test_verified_staging_path_cannot_publish_replacement_bytes(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "deliverable.md").write_text("WRONG\n", encoding="utf-8")
    output = tmp_path / "certified.md"
    wrapper = CertifiedArtifactWrapper(
        contract=ArtifactContract(
            output_path=output,
            workspace_root=workspace,
            artifact_path=Path("deliverable.md"),
            criteria=(ExactCountCriterion("marker", "MARKER", 1),),
        ),
        ledger_path=tmp_path / "certifications.db",
    )
    real_replace = os.replace

    def replace_with_attacker_bytes(source, destination):
        source = Path(source)
        source.unlink()
        source.write_text("ATTACKER MARKER\n", encoding="utf-8")
        return real_replace(source, destination)

    monkeypatch.setattr("agent.artifact_certification.os.replace", replace_with_attacker_bytes)

    try:
        result = wrapper.run(run_id="publication-race", draft="irrelevant")
    except RuntimeError:
        result = None

    assert not output.exists() or output.read_bytes() == b"WRONG\n"
    if result is not None:
        assert result.status == "FAIL"


def test_output_is_opened_once_for_check_and_write(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "deliverable.md").write_text("MARKER\n", encoding="utf-8")
    output = tmp_path / "certified.md"
    wrapper = CertifiedArtifactWrapper(
        contract=ArtifactContract(
            output_path=output,
            workspace_root=workspace,
            artifact_path=Path("deliverable.md"),
            criteria=(ExactCountCriterion("marker", "MARKER", 1),),
        ),
        ledger_path=tmp_path / "certifications.db",
    )
    real_open = os.open
    output_opens = 0

    def count_output_opens(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal output_opens
        if dir_fd is None and os.fspath(path) == os.fspath(output):
            output_opens += 1
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("agent.artifact_certification.os.open", count_output_opens)

    result = wrapper.run(run_id="single-output-open", draft="irrelevant")

    assert result.status == "PASS"
    assert output.read_bytes() == b"MARKER\n"
    assert output_opens == 1


def test_same_wrapper_retry_after_pre_pending_failure_cannot_change_bytes(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "deliverable.md"
    artifact.write_text("FIRST MARKER\n", encoding="utf-8")
    wrapper = CertifiedArtifactWrapper(
        contract=ArtifactContract(
            output_path=tmp_path / "certified.md",
            workspace_root=workspace,
            artifact_path=Path("deliverable.md"),
            criteria=(ExactCountCriterion("marker", "MARKER", 1),),
        ),
        ledger_path=tmp_path / "certifications.db",
    )
    real_stage = __import__(
        "agent.artifact_certification", fromlist=["_stage_exact_path"]
    )._stage_exact_path
    monkeypatch.setattr(
        "agent.artifact_certification._stage_exact_path",
        lambda *_args: (_ for _ in ()).throw(OSError("pre-pending")),
    )
    with pytest.raises(OSError, match="pre-pending"):
        wrapper.run(run_id="same-wrapper-retry", draft="irrelevant")

    artifact.write_text("REPLACEMENT MARKER\n", encoding="utf-8")
    monkeypatch.setattr("agent.artifact_certification._stage_exact_path", real_stage)
    with pytest.raises(sqlite3.IntegrityError, match="already reserved"):
        wrapper.run(run_id="same-wrapper-retry", draft="irrelevant")
