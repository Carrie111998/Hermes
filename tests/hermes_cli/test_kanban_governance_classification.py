from hermes_cli import kanban_governance as kg


def test_classify_blocker_dependency_wait_when_parent_artifact_missing():
    blocker = kg.classify_blocker(
        reason="parent report missing at /mnt/pop/got-ocr2-llama-cpp-test/GOT-OCR2_LLAMA_CPP_VALIDATION_REPORT.md",
        task_title="Measure GOT-OCR2.0 CER",
        task_body="Read parent artifacts before escalating",
        attachments_checked=True,
        parents_checked=True,
        manuals_checked=True,
        sessions_checked=False,
        has_existing_artifact=True,
    )
    assert blocker == "dependency_wait"


def test_classify_blocker_lane_work_for_missing_manual_or_bad_path():
    blocker = kg.classify_blocker(
        reason="PIPELINE_AND_AGENTS.md not found in searched tree",
        task_title="Audit pipeline run statuses",
        task_body="Locate the documented count before blocking",
        attachments_checked=False,
        parents_checked=False,
        manuals_checked=False,
        sessions_checked=False,
        has_existing_artifact=False,
    )
    assert blocker == "lane_work"


def test_exception_packet_is_decision_shaped_not_replay_shaped():
    packet = kg.build_exception_packet(
        task_id="t_deadbeef",
        board="ocr",
        blocker_class="human_decision",
        reason="Need principal choice between Transformers native and got.cpp hybrid",
        checks=["attachments checked", "parent artifact checked", "manuals loaded"],
        decision_prompt="Choose runtime A or runtime B",
    )
    assert "t_deadbeef" in packet
    assert "Choose runtime A or runtime B" in packet
    assert "restate the research" not in packet.lower()
    assert "what should we do" not in packet.lower()
