from pathlib import Path


SKILL_PATH = (
    Path(__file__).resolve().parents[2] / "skills" / "muncho-mac-ops-edge" / "SKILL.md"
)


def test_mac_ops_edge_preserves_model_authored_progress_and_steering():
    instructions = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())

    assert "`mac_ops_task_read` as a bounded observation" in instructions
    assert "return control to the model after each read" in instructions
    assert "Do not replace the structured read loop" in instructions
    assert "`--wait-closed`" in instructions
    assert "never copy raw heartbeat lines" in instructions


def test_mac_ops_edge_keeps_cloud_capable_work_off_the_local_mac():
    instructions = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())

    assert "Check the normal least-privilege cloud path first" in instructions
    assert "selected only after the cloud-path check" in instructions
    assert "If no listed Mac-only capability is required, do not submit" in instructions
    assert "Git/GitLab, GCP API, and generic CLI work are not Mac-only" in instructions
