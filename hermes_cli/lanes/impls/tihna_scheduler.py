"""Ready-to-schedule Tihna ingest and weekly digest callables."""

from __future__ import annotations

from datetime import datetime, timezone
import time

from hermes_cli.cost.kill_switch import KillSwitchTripped
from hermes_cli.lanes.contracts import LaneTask
from hermes_cli.lanes.impls.tihna import TihnaLane


def _week_external_id() -> str:
    year, week, _ = datetime.now(timezone.utc).isocalendar()
    return f"digest-{year}-W{week:02d}"


def default_llm_caller(
    *,
    prompt: str,
    max_tokens: int,
    route: dict,
    **_kwargs,
) -> dict:
    """Call the routed provider without bypassing LaneHarness accounting."""
    from agent.auxiliary_client import (
        _build_call_kwargs,
        _get_cached_client,
        extract_content_or_reasoning,
    )
    from agent.usage_pricing import normalize_usage

    provider = str(route["provider"])
    model = str(route["model"])
    client, resolved_model = _get_cached_client(
        provider,
        model=model,
        task="tihna",
    )
    if client is None or not resolved_model:
        raise RuntimeError(
            f"Tihna LLM provider is unavailable: {provider}/{model}"
        )
    base_url = str(getattr(client, "base_url", "") or "")
    call_kwargs = _build_call_kwargs(
        provider,
        resolved_model,
        [{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        timeout=120.0,
        base_url=base_url,
    )
    started = time.monotonic()
    response = client.chat.completions.create(**call_kwargs)
    latency_ms = int((time.monotonic() - started) * 1000)
    usage = normalize_usage(
        getattr(response, "usage", None),
        provider=provider,
    )
    return {
        "text": extract_content_or_reasoning(response),
        "provider": provider,
        "model": str(getattr(response, "model", "") or resolved_model),
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "latency_ms": latency_ms,
        "outcome": "success",
    }


def run_ingest(*, lane: TihnaLane, harness) -> int:
    control = LaneTask(
        lane_id="tihna",
        external_id="ingest-run",
        payload={"stage": "ingest"},
    )
    harness.admit(task=control, apply_rate_limits=False)
    return len(lane.ingest(harness=harness))


def run_digest(*, lane: TihnaLane, harness):
    external_id = _week_external_id()
    control = harness.find_task(external_id=external_id)
    if control is None:
        control = harness.persist_task(
            LaneTask(
                lane_id="tihna",
                external_id=external_id,
                task_id=f"tihna-{external_id}",
                payload={"stage": "digest"},
            )
        )
    try:
        harness.admit(task=control)
        classify_task = LaneTask(
            lane_id="tihna",
            external_id=control.external_id,
            task_id=control.task_id,
            id=control.id,
            payload={"stage": "classify"},
            status=control.status,
        )
        classification = lane.draft(task=classify_task, harness=harness)
        digest_task = LaneTask(
            lane_id="tihna",
            external_id=control.external_id,
            task_id=control.task_id,
            id=control.id,
            payload={
                "stage": "digest",
                "ranked_items": classification.metadata.get(
                    "ranked_items",
                    [],
                ),
            },
            status=control.status,
        )
        digest = lane.draft(task=digest_task, harness=harness)
        request = lane.approve(
            task=digest_task,
            draft=digest,
            harness=harness,
        )
        cleanup_task = LaneTask(
            lane_id="tihna",
            external_id=digest_task.external_id,
            task_id=digest_task.task_id,
            id=digest_task.id,
            payload={
                **digest_task.payload,
                "ranked_items": digest.metadata.get("ranked_items", []),
                "ingested_this_window": digest.metadata.get(
                    "total_ingested_this_week",
                    0,
                ),
            },
            status=digest_task.status,
        )
        lane.cleanup(task=cleanup_task, harness=harness)
        return request
    except KillSwitchTripped:
        if control.id is not None:
            harness.update_task(task=control, status="failed")
        raise


__all__ = ["default_llm_caller", "run_digest", "run_ingest"]
