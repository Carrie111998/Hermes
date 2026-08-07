from pathlib import Path


SKILL_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "muncho-mac-ops-edge"
    / "SKILL.md"
)


def test_mac_ops_edge_preserves_model_authored_progress_and_steering():
    instructions = " ".join(
        SKILL_PATH.read_text(encoding="utf-8").split()
    )

    assert "`mac_ops_task_read` as a bounded observation" in instructions
    assert "return control to the model after each read" in instructions
    assert "Do not replace the structured read loop" in instructions
    assert "`--wait-closed`" in instructions
    assert "never copy raw heartbeat lines" in instructions
