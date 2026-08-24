"""Sampling bounds in trajectory_compressor single-file mode (#93737).

The single-file branch sampled `max(1, ...)` entries without bounding by
the population, so an empty input (or one holding only invalid JSON
lines) crashed `random.sample` with "Sample larger than population",
aborting unattended batch runs. The directory branch already bounded with
`min(sample_size, len(entries))` — these tests pin the mirrored guard and
the unchanged non-empty behavior by driving main() directly in dry-run
mode (which exercises the sampling block and returns before any
model/tokenizer dependency).
"""

import json

import trajectory_compressor


def test_empty_input_with_sample_percent_exits_cleanly(tmp_path, capsys):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")

    trajectory_compressor.main(input=str(empty), sample_percent=50, dry_run=True)
    out = capsys.readouterr().out

    assert "Loaded 0 trajectories" in out
    assert "Sampled 0 trajectories (50% of 0)" in out


def test_invalid_lines_only_input_exits_cleanly(tmp_path, capsys):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\nalso not json\n", encoding="utf-8")

    trajectory_compressor.main(input=str(bad), sample_percent=25, dry_run=True)
    out = capsys.readouterr().out

    assert "Sampled 0 trajectories" in out


def test_nonempty_input_still_samples_requested_percent(tmp_path, capsys):
    good = tmp_path / "good.jsonl"
    good.write_text(
        "\n".join(json.dumps({"id": i}) for i in range(4)) + "\n",
        encoding="utf-8",
    )

    trajectory_compressor.main(input=str(good), sample_percent=50, dry_run=True)
    out = capsys.readouterr().out

    assert "Sampled 2 trajectories (50% of 4)" in out


def test_directory_mode_empty_files_remain_clean(tmp_path, capsys):
    """The already-guarded directory branch keeps its behavior when the
    directory holds only empty JSONL files."""
    data = tmp_path / "runs"
    data.mkdir()
    (data / "a.jsonl").write_text("", encoding="utf-8")

    trajectory_compressor.main(input=str(data), sample_percent=50, dry_run=True)
    out = capsys.readouterr().out

    assert "Sampled 0 from 0 total trajectories" in out
