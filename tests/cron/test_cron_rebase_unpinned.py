"""Official model/provider writes rebase unpinned cron snapshots.

The #44585 fire-time guard still fails closed for ambient drift (hand-edited
config, env-only swaps). Operator-facing writes through hermes config /
hermes model / TUI /model must rebase snapshots so a preset rename does
not skip the next tick.
"""

from __future__ import annotations

from typing import Any

import cron.jobs as jobs


def _isolate_storage(monkeypatch, existing: list[dict[str, Any]]):
    store = {"jobs": [dict(job) for job in existing]}

    def _load() -> list[dict[str, Any]]:
        return [dict(job) for job in store["jobs"]]

    def _save(updated, **_kwargs) -> None:
        store["jobs"] = [dict(job) for job in updated]

    monkeypatch.setattr(jobs, "load_jobs", _load, raising=True)
    monkeypatch.setattr(jobs, "save_jobs", _save, raising=True)
    return store


def test_rebases_unpinned_model_snapshot_and_clears_alert(monkeypatch) -> None:
    store = _isolate_storage(
        monkeypatch,
        [
            {
                "id": "drain",
                "enabled": True,
                "model": None,
                "provider": None,
                "model_snapshot": "grok-4.5->m3",
                "provider_snapshot": "moa",
                "drift_alerted": True,
            }
        ],
    )

    rewritten = jobs.rebase_unpinned_inference_snapshots(model="m3->grok-4.6")

    assert rewritten == ["drain"]
    job = store["jobs"][0]
    assert job["model_snapshot"] == "m3->grok-4.6"
    assert job["provider_snapshot"] == "moa"
    assert "drift_alerted" not in job


def test_leaves_pinned_no_agent_and_matching_snapshots_alone(monkeypatch) -> None:
    store = _isolate_storage(
        monkeypatch,
        [
            {
                "id": "pinned",
                "enabled": True,
                "model": "keep-me",
                "model_snapshot": "old-model",
            },
            {
                "id": "script",
                "enabled": True,
                "no_agent": True,
                "model_snapshot": "old-model",
            },
            {
                "id": "already-current",
                "enabled": True,
                "model": None,
                "model_snapshot": "m3->grok-4.6",
            },
            {
                "id": "paused",
                "enabled": False,
                "state": "paused",
                "model_snapshot": "old-model",
            },
        ],
    )

    rewritten = jobs.rebase_unpinned_inference_snapshots(model="m3->grok-4.6")

    assert rewritten == []
    assert store["jobs"][0]["model_snapshot"] == "old-model"
    assert store["jobs"][1]["model_snapshot"] == "old-model"
    assert store["jobs"][2]["model_snapshot"] == "m3->grok-4.6"
    assert store["jobs"][3]["model_snapshot"] == "old-model"


def test_provider_axis_is_independent_of_model(monkeypatch) -> None:
    store = _isolate_storage(
        monkeypatch,
        [
            {
                "id": "both",
                "enabled": True,
                "model": None,
                "provider": None,
                "model_snapshot": "old-model",
                "provider_snapshot": "openrouter",
            }
        ],
    )

    rewritten = jobs.rebase_unpinned_inference_snapshots(provider="moa")

    assert rewritten == ["both"]
    job = store["jobs"][0]
    assert job["provider_snapshot"] == "moa"
    assert job["model_snapshot"] == "old-model"
