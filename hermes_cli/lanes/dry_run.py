"""Fixture-backed full-lane simulation with zero durable side effects."""

from __future__ import annotations

import importlib
import importlib.util
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Literal

import yaml

from hermes_constants import get_default_hermes_root
from hermes_cli.lanes.harness import DryRunHarness, DryRunViolation
from hermes_cli.lanes.manifest import default_path, validate_manifest

DryRunStage = Literal["ingest", "digest", "full"]

_DIGEST = """# Tihna Weekly Trends — 2026-W30

## Signal Summary
Synthetic fixture summary.

## Notable Papers
- Synthetic paper

## Community Chatter
- Synthetic community item

## Adjacent Tech
- Synthetic adjacent technology

## Recommended Follow-ups
1. Read the synthetic primary source.
"""


@dataclass(frozen=True)
class LaneDryRunReport:
    lane_id: str
    stage: str
    success: bool
    ingested: int = 0
    classified: int = 0
    drafted: int = 0
    approvals_enqueued: int = 0
    publish_would_have_been_called: bool = False
    simulated_llm_cost_aud: float = 0.0
    simulated_write_call_count: int = 0
    kanban_writes: int = 0
    cost_ledger_writes: int = 0
    side_effect_writes: int = 0
    fixture_feed_used: bool = False
    fake_llm_used: bool = False
    error: str | None = None

    @property
    def exit_code(self) -> int:
        return 0 if self.success else 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
        )


class FakeFeedFetcher:
    def __init__(self, fixture_path: str | Path) -> None:
        self.fixture_path = Path(fixture_path)
        self.calls = 0

    def __call__(
        self,
        feed_url: str,
        *,
        category: str = "other",
        now=None,
        metric=None,
        **_kwargs,
    ) -> list[dict[str, Any]]:
        from hermes_cli.lanes.impls.tihna_rss import (
            fetch_feed_from_bytes,
        )

        self.calls += 1
        if self.calls > 1:
            return []
        return fetch_feed_from_bytes(
            self.fixture_path.read_bytes(),
            feed_url=feed_url,
            category=category,
            now=now,
            metric=metric,
        )


class FakeLLMCaller:
    def __init__(self, *, cost_per_call_aud: float = 0.01) -> None:
        self.cost_per_call_aud = float(cost_per_call_aud)
        self.calls: list[str] = []

    def __call__(
        self,
        *,
        prompt: str,
        purpose: str,
        **_kwargs,
    ) -> dict[str, Any]:
        self.calls.append(purpose)
        if purpose == "classification":
            marker = "Items to score:\n"
            raw_items = prompt.split(marker, 1)[1]
            items = json.loads(raw_items)
            text = json.dumps(
                [
                    {
                        "external_id": item["external_id"],
                        "score": 75,
                        "reason": "synthetic dry-run signal",
                    }
                    for item in items
                ],
                sort_keys=True,
            )
        elif purpose == "draft":
            text = _DIGEST
        else:
            text = "Synthetic dry-run response."
        return {
            "text": text,
            "provider": "dry-run",
            "model": "fixture",
            "simulated_cost_aud": self.cost_per_call_aud,
        }


def _default_fixture() -> Path:
    return (
        Path(__file__).parents[2]
        / "tests"
        / "fixtures"
        / "tihna_rss"
        / "valid_atom.xml"
    )


def _failure_report(
    *,
    lane_id: str,
    stage: str,
    error: str,
) -> LaneDryRunReport:
    return LaneDryRunReport(
        lane_id=lane_id,
        stage=stage,
        success=False,
        error=error,
    )


def run_lane_dry_run(
    lane_id: str,
    *,
    stage: DryRunStage = "full",
    manifest_path: str | Path | None = None,
    db_path: str | Path | None = None,
    fixture_path: str | Path | None = None,
    feed_fetcher: Callable[..., list[dict[str, Any]]] | None = None,
    llm_caller: Callable[..., dict[str, Any]] | None = None,
    harness_factory: Callable[..., DryRunHarness] = DryRunHarness,
) -> LaneDryRunReport:
    normalized = str(lane_id).strip().lower()
    if stage not in {"ingest", "digest", "full"}:
        return _failure_report(
            lane_id=normalized,
            stage=str(stage),
            error=f"unsupported dry-run stage: {stage}",
        )
    source_manifest = (
        Path(manifest_path).expanduser()
        if manifest_path is not None
        else default_path()
    )
    source_db = (
        Path(db_path).expanduser()
        if db_path is not None
        else get_default_hermes_root() / "kanban.db"
    )
    try:
        manifest = validate_manifest(
            yaml.safe_load(
                source_manifest.read_text(encoding="utf-8")
            )
        )
    except Exception as exc:
        return _failure_report(
            lane_id=normalized,
            stage=stage,
            error=f"manifest error: {type(exc).__name__}: {exc}",
        )
    config = manifest.by_id().get(normalized)
    if config is None:
        return _failure_report(
            lane_id=normalized,
            stage=stage,
            error=f"unknown lane: {normalized}",
        )
    try:
        spec = importlib.util.find_spec(config.module)
    except (ImportError, ModuleNotFoundError, ValueError):
        spec = None
    if spec is None:
        return _failure_report(
            lane_id=normalized,
            stage=stage,
            error=f"module_status=ABSENT: {config.module}",
        )

    fake_feed = feed_fetcher or FakeFeedFetcher(
        fixture_path or _default_fixture()
    )
    fake_llm = llm_caller or FakeLLMCaller()
    try:
        harness = harness_factory(
            lane_id=normalized,
            db_path=source_db,
            manifest_path=source_manifest,
            llm_caller=fake_llm,
        )
    except Exception as exc:
        return _failure_report(
            lane_id=normalized,
            stage=stage,
            error=f"harness error: {type(exc).__name__}: {exc}",
        )

    module = importlib.import_module(config.module)
    factory = getattr(module, "build_lane", None)
    if not callable(factory):
        return _failure_report(
            lane_id=normalized,
            stage=stage,
            error=f"lane module has no build_lane(): {config.module}",
        )
    lane = factory()
    scheduler = importlib.import_module(f"{config.module}_scheduler")
    rss_module = None
    original_fetch = None
    if normalized == "tihna":
        rss_module = importlib.import_module(
            "hermes_cli.lanes.impls.tihna_rss"
        )
        original_fetch = rss_module.fetch_feed
        rss_module.fetch_feed = fake_feed

    ingested = 0
    try:
        if stage in {"ingest", "full"}:
            ingested = int(
                scheduler.run_ingest(lane=lane, harness=harness)
            )
        if stage == "digest":
            ingested = int(
                scheduler.run_ingest(lane=lane, harness=harness)
            )
        if stage in {"digest", "full"}:
            scheduler.run_digest(lane=lane, harness=harness)
        harness.assert_zero_real_writes()
    except DryRunViolation:
        raise
    except Exception as exc:
        return LaneDryRunReport(
            lane_id=normalized,
            stage=stage,
            success=False,
            ingested=ingested,
            simulated_llm_cost_aud=harness.simulated_cost_aud,
            simulated_write_call_count=harness.simulated_write_calls,
            kanban_writes=harness.write_calls,
            cost_ledger_writes=harness.cost_ledger_writes,
            side_effect_writes=harness.side_effect_writes,
            fixture_feed_used=bool(
                getattr(fake_feed, "calls", 0)
            ),
            fake_llm_used=bool(getattr(fake_llm, "calls", [])),
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if rss_module is not None and original_fetch is not None:
            rss_module.fetch_feed = original_fetch

    classified = len(
        [
            task
            for task in harness.tasks
            if task.payload.get("stage") == "item"
            and "score" in task.payload
        ]
    )
    drafted = len(
        [
            call
            for call in harness.llm_calls
            if call["purpose"] == "draft"
        ]
    )
    return LaneDryRunReport(
        lane_id=normalized,
        stage=stage,
        success=True,
        ingested=ingested,
        classified=classified,
        drafted=drafted,
        approvals_enqueued=len(harness.approvals),
        publish_would_have_been_called=(
            harness.publish_would_have_been_called
        ),
        simulated_llm_cost_aud=harness.simulated_cost_aud,
        simulated_write_call_count=harness.simulated_write_calls,
        kanban_writes=harness.write_calls,
        cost_ledger_writes=harness.cost_ledger_writes,
        side_effect_writes=harness.side_effect_writes,
        fixture_feed_used=bool(getattr(fake_feed, "calls", 0)),
        fake_llm_used=bool(getattr(fake_llm, "calls", [])),
    )


__all__ = [
    "DryRunStage",
    "FakeFeedFetcher",
    "FakeLLMCaller",
    "LaneDryRunReport",
    "run_lane_dry_run",
]
