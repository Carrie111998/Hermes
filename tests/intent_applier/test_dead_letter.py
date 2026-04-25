import json
from pathlib import Path

from intent_applier.dead_letter import write_dead_letter


class TestWriteDeadLetter:
    def test_moves_file_and_writes_sidecar(self, tmp_path: Path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        dead_letter = tmp_path / "dead-letter"
        intent_file = inbox / "20260425T100000_APPROVAL_INTENT_main.json"
        intent_file.write_text('{"job_id":"j-1"}', encoding="utf-8")

        write_dead_letter(
            intent_file,
            dead_letter_dir=dead_letter,
            error_class="JobOpsClientPermanentError",
            error_message="400 Bad Request",
            stack_trace="Traceback...\n  fail()\n",
            retry_count=0,
        )

        # Original moved
        assert not intent_file.exists()
        moved = dead_letter / intent_file.name
        assert moved.exists()
        # Sidecar has structured error info
        sidecar = dead_letter / f"{intent_file.name}.error.json"
        assert sidecar.exists()
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        assert data["error_class"] == "JobOpsClientPermanentError"
        assert data["error_message"] == "400 Bad Request"
        assert data["retry_count"] == 0
        assert "stack_trace" in data
        assert "last_attempt_at" in data

    def test_creates_dead_letter_dir(self, tmp_path: Path):
        intent_file = tmp_path / "intent.json"
        intent_file.write_text("{}", encoding="utf-8")
        dead_letter = tmp_path / "dl-nested" / "dead-letter"
        write_dead_letter(
            intent_file, dead_letter_dir=dead_letter,
            error_class="X", error_message="y", stack_trace="", retry_count=0,
        )
        assert dead_letter.exists()
        assert (dead_letter / "intent.json").exists()
