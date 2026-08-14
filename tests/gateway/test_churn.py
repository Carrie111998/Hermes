"""Gateway churn record durability and concurrency contract."""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ProcessPoolExecutor

from gateway import churn


def _records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _append_replacement(pid):
    return churn.append_gateway_churn_event(
        "replace", pid_old=pid, pid_new=pid + 10_000
    )


def test_hook_info_is_side_effect_free(tmp_path, monkeypatch):
    path = tmp_path / "gateway-churn.jsonl"
    monkeypatch.setenv(churn.GATEWAY_CHURN_PATH_ENV, str(path))

    info = churn.gateway_churn_hook_info()

    assert info == {
        "version": 1,
        "enabled": True,
        "path": str(path),
        "max_records": churn.GATEWAY_CHURN_MAX_RECORDS,
    }
    assert not path.exists()


def test_racing_writers_preserve_every_event(tmp_path, monkeypatch):
    path = tmp_path / "gateway-churn.jsonl"
    monkeypatch.setenv(churn.GATEWAY_CHURN_PATH_ENV, str(path))

    with ProcessPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_append_replacement, range(1, 65)))

    records = _records(path)
    assert all(results)
    assert len(records) == 64
    assert {record["pid_old"] for record in records} == set(range(1, 65))
    assert all(set(record) == {"event_type", "timestamp", "pid_old", "pid_new"} for record in records)


def test_record_drops_oldest_at_cap(tmp_path, monkeypatch):
    path = tmp_path / "gateway-churn.jsonl"
    monkeypatch.setenv(churn.GATEWAY_CHURN_PATH_ENV, str(path))

    total = churn.GATEWAY_CHURN_MAX_RECORDS + 25
    for pid in range(1, total + 1):
        assert churn.append_gateway_churn_event("start", pid_old=None, pid_new=pid)

    records = _records(path)
    assert len(records) == churn.GATEWAY_CHURN_MAX_RECORDS
    assert [record["pid_new"] for record in records] == list(
        range(total - churn.GATEWAY_CHURN_MAX_RECORDS + 1, total + 1)
    )


def test_reader_never_sees_partial_window(tmp_path, monkeypatch):
    path = tmp_path / "gateway-churn.jsonl"
    monkeypatch.setenv(churn.GATEWAY_CHURN_PATH_ENV, str(path))
    assert churn.append_gateway_churn_event("start", pid_old=None, pid_new=1)
    finished = threading.Event()
    failures: list[BaseException] = []

    def writer():
        try:
            for pid in range(2, 350):
                assert churn.append_gateway_churn_event(
                    "replace", pid_old=pid - 1, pid_new=pid
                )
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=writer)
    thread.start()
    reads = 0
    while not finished.is_set():
        raw = path.read_bytes()
        assert raw.endswith(b"\n")
        lines = raw.splitlines()
        assert 1 <= len(lines) <= churn.GATEWAY_CHURN_MAX_RECORDS
        assert all(isinstance(json.loads(line), dict) for line in lines)
        reads += 1
    thread.join()

    assert not failures
    assert reads > 0


def test_takeover_marker_contract_remains_separate():
    from gateway import status

    assert status.write_takeover_marker is not churn.append_gateway_churn_event
    assert status.consume_takeover_marker_for_self.__module__ == "gateway.status"
