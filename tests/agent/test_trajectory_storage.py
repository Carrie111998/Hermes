"""Tests for lossless trajectory storage formats."""

import gzip
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent.trajectory import save_trajectory
import trajectory_compressor
from trajectory_compressor import _default_output_path, _open_jsonl


def test_default_trajectory_output_is_gzip_jsonl(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    save_trajectory([{"from": "human", "value": "hello"}], "test-model", True)

    path = tmp_path / "trajectory_samples.jsonl.gz"
    assert path.is_file()
    assert not (tmp_path / "trajectory_samples.jsonl").exists()
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        entry = json.loads(stream.readline())
    assert entry["conversations"] == [{"from": "human", "value": "hello"}]
    assert entry["completed"] is True


def test_explicit_plain_jsonl_path_remains_backward_compatible(tmp_path):
    path = tmp_path / "legacy.jsonl"

    save_trajectory([{"from": "gpt", "value": "done"}], "test-model", True, str(path))

    assert json.loads(path.read_text(encoding="utf-8"))["model"] == "test-model"


def test_explicit_gzip_jsonl_path_is_supported(tmp_path):
    path = tmp_path / "custom.jsonl.gz"

    save_trajectory([{"from": "tool", "value": "result"}], "test-model", False, str(path))

    with gzip.open(path, "rt", encoding="utf-8") as stream:
        assert json.loads(stream.readline())["completed"] is False


def test_pathlike_gzip_filename_is_supported(tmp_path):
    path = Path(tmp_path) / "pathlike.jsonl.gz"

    save_trajectory([], "test-model", True, path)

    with gzip.open(path, "rt", encoding="utf-8") as stream:
        assert json.loads(stream.readline())["model"] == "test-model"


def test_trajectory_compressor_reader_accepts_gzip_jsonl(tmp_path):
    path = tmp_path / "trajectories.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        stream.write('{"conversations": []}\n')

    with _open_jsonl(path, "rt") as stream:
        assert json.loads(stream.readline())["conversations"] == []


def test_compressed_input_gets_clean_default_output_name(tmp_path):
    input_path = tmp_path / "trajectories.jsonl.gz"

    assert _default_output_path(input_path) == tmp_path / "trajectories_compressed.jsonl"


def test_directory_sampling_reads_gzip_jsonl_after_writing_sample(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    source = input_dir / "trajectories.jsonl.gz"
    with gzip.open(source, "wt", encoding="utf-8") as stream:
        stream.write('{"conversations": []}\n')

    class FakeCompressor:
        def __init__(self, config):
            pass

        def process_directory(self, sampled_dir, output_dir):
            sampled = sampled_dir / source.name
            with trajectory_compressor._open_jsonl(sampled) as stream:
                assert json.loads(stream.readline())["conversations"] == []

    monkeypatch.setattr(trajectory_compressor, "TrajectoryCompressor", FakeCompressor)
    trajectory_compressor.main(
        input=str(input_dir),
        output=str(tmp_path / "output"),
        sample_percent=100,
    )


def test_concurrent_default_writes_remain_readable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    payloads = [
        [{"from": "tool", "value": f"result-{index}-" + ("x" * 100_000)}]
        for index in range(8)
    ]

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda trajectory: save_trajectory(trajectory, "test-model", True),
                payloads,
            )
        )

    with gzip.open(tmp_path / "trajectory_samples.jsonl.gz", "rt", encoding="utf-8") as stream:
        entries = [json.loads(line) for line in stream if line.strip()]
    assert len(entries) == len(payloads)
