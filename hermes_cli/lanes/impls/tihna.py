"""Tihna public-RSS trend research lane."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from hermes_constants import get_default_hermes_root
from hermes_cli.lanes.contracts import (
    ApprovalGrant,
    ApprovalRequest,
    LaneDraft,
    LaneTask,
    PublishResult,
)
from hermes_cli.lanes.impls import tihna_rss
from hermes_cli.lanes.impls.tihna_classifier import (
    build_classify_prompt,
    parse_scores,
)
from hermes_cli.lanes.impls.tihna_templates import (
    DIGEST_PROMPT,
    SECTION_HEADINGS,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _week_label(value: datetime | None = None) -> str:
    current = (value or _now()).astimezone(timezone.utc)
    iso_year, iso_week, _ = current.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


class TihnaLane:
    lane_id = "tihna"
    name = "Tihna Trend Research"
    version = "1.0.0"

    def __init__(
        self,
        *,
        feeds_path: str | Path | None = None,
        output_root: str | Path | None = None,
        now_fn=_now,
    ) -> None:
        self.feeds_path = (
            Path(feeds_path)
            if feeds_path
            else Path(__file__).with_name("tihna_feeds.yaml")
        )
        self.output_root = (
            Path(output_root)
            if output_root
            else get_default_hermes_root() / "tihna-digests"
        )
        self.now_fn = now_fn

    def _feeds(self) -> list[dict[str, str]]:
        raw = yaml.safe_load(self.feeds_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ValueError("Tihna feed schema_version must be 1")
        feeds = raw.get("feeds")
        if not isinstance(feeds, list):
            raise ValueError("Tihna feeds must be a list")
        normalized = []
        for item in feeds:
            if not isinstance(item, dict):
                raise ValueError("Tihna feed entries must be mappings")
            name = str(item.get("name") or "").strip()
            url = str(item.get("url") or "").strip()
            category = str(item.get("category") or "").strip()
            if not name or not url or not category:
                raise ValueError("Tihna feed name/url/category are required")
            normalized.append(
                {"name": name, "url": url, "category": category}
            )
        return normalized

    def _metric(self, harness, task: LaneTask, name: str, value: float) -> None:
        harness.record_metric(
            task=task,
            metric_name=name,
            value=float(value),
        )

    def ingest(self, *, harness) -> list[LaneTask]:
        current = self.now_fn().astimezone(timezone.utc)
        cutoff = current - timedelta(days=8)
        created: list[LaneTask] = []
        filtered = 0
        metric_task = LaneTask(
            lane_id=self.lane_id,
            external_id="tihna-ingest-metrics",
            payload={"stage": "ingest"},
        )

        def feed_metric(name: str, value: float) -> None:
            self._metric(harness, metric_task, name, value)

        for feed in self._feeds():
            entries = tihna_rss.fetch_feed(
                feed["url"],
                category=feed["category"],
                now=current,
                metric=feed_metric,
            )
            for entry in entries:
                try:
                    published = datetime.fromisoformat(
                        str(entry["pub_date"]).replace("Z", "+00:00")
                    ).astimezone(timezone.utc)
                except (KeyError, TypeError, ValueError):
                    filtered += 1
                    continue
                if (
                    published < cutoff
                    or len(str(entry.get("summary") or "")) < 100
                    or not str(entry.get("title") or "").strip()
                ):
                    filtered += 1
                    continue
                external_id = str(entry["external_id"])
                if harness.find_task(external_id=external_id) is not None:
                    continue
                harness.check_rate_limit(
                    window_kind="hourly_ingest",
                    increment=1,
                )
                task = harness.persist_task(
                    LaneTask(
                        lane_id=self.lane_id,
                        external_id=external_id,
                        task_id=f"tihna-{external_id}",
                        payload={
                            **entry,
                            "feed_name": feed["name"],
                            "stage": "item",
                        },
                    )
                )
                created.append(task)
        if created:
            self._metric(
                harness,
                metric_task,
                "items_ingested",
                len(created),
            )
        if filtered:
            self._metric(
                harness,
                metric_task,
                "items_filtered_out",
                filtered,
            )
        return created

    def _classify(self, task: LaneTask, harness) -> LaneDraft:
        since = _iso(self.now_fn() - timedelta(days=7))
        candidates = [
            item
            for item in harness.list_tasks(
                status="ingested",
                ingested_since=since,
            )
            if item.payload.get("stage") == "item"
        ]
        if not candidates:
            return LaneDraft(
                content="[]",
                metadata={"kind": "classification", "ranked_items": []},
            )
        result = harness.call_llm(
            task=task,
            prompt=build_classify_prompt(candidates),
            max_tokens=800,
            purpose="classification",
        )
        scores = parse_scores(result.text)
        by_external_id = {
            item["external_id"]: item for item in scores
        }
        ranked = []
        for candidate in candidates:
            score = by_external_id.get(candidate.external_id)
            if score is None:
                continue
            payload = {
                **candidate.payload,
                "score": score["score"],
                "score_reason": score["reason"],
            }
            harness.update_task(task=candidate, payload=payload)
            ranked.append({**payload, "external_id": candidate.external_id})
        return LaneDraft(
            content=json.dumps(
                ranked,
                ensure_ascii=False,
                sort_keys=True,
            ),
            metadata={"kind": "classification", "ranked_items": ranked},
        )

    def _digest(self, task: LaneTask, harness) -> LaneDraft:
        since = _iso(self.now_fn() - timedelta(days=7))
        candidates = [
            {**item.payload, "external_id": item.external_id}
            for item in harness.list_tasks(
                status="ingested",
                ingested_since=since,
            )
            if item.payload.get("stage") == "item"
            and int(item.payload.get("score") or 0) >= 60
        ]
        week_label = _week_label(self.now_fn())
        prompt = (
            DIGEST_PROMPT.replace("<WEEK_LABEL>", week_label).replace(
                "<RANKED_ITEMS>",
                json.dumps(
                    candidates,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )
        result = harness.call_llm(
            task=task,
            prompt=prompt,
            max_tokens=2500,
            purpose="draft",
        )
        markdown = harness.lint_draft(result.text)
        ingested_this_week = [
            item
            for item in harness.list_tasks(
                status="ingested",
                ingested_since=since,
            )
            if item.payload.get("stage") == "item"
        ]
        return LaneDraft(
            content=markdown,
            metadata={
                "kind": "digest",
                "week_label": week_label,
                "sections": list(SECTION_HEADINGS),
                "section_summaries": list(SECTION_HEADINGS),
                "selected_item_count": len(candidates),
                "total_ingested_this_week": len(ingested_this_week),
                "ranked_items": candidates,
            },
        )

    def draft(
        self,
        *,
        task: LaneTask,
        harness,
    ) -> LaneDraft:
        stage = str(task.payload.get("stage") or "")
        if stage == "classify":
            return self._classify(task, harness)
        if stage == "digest":
            return self._digest(task, harness)
        raise ValueError(f"unsupported Tihna draft stage: {stage}")

    def approve(
        self,
        *,
        task: LaneTask,
        draft: LaneDraft,
        harness,
    ) -> ApprovalRequest:
        if task.payload.get("stage") != "digest":
            raise ValueError("only a Tihna digest can be approved")
        summary = (
            f"Tihna digest for {draft.metadata.get('week_label')} — "
            f"{draft.metadata.get('selected_item_count', 0)} items across "
            f"{len(draft.metadata.get('sections') or [])} sections"
        )
        envelope = LaneDraft(
            content=draft.content,
            metadata={
                **draft.metadata,
                "summary": summary,
                "draft_body_markdown": draft.content,
                "preview": draft.content[:500],
            },
        )
        return harness.enqueue_approval(task=task, draft=envelope)

    def _next_output_path(self) -> Path:
        current = self.now_fn().astimezone(timezone.utc)
        iso_year, iso_week, _ = current.isocalendar()
        base = self.output_root / f"{iso_year}-{iso_week:02d}-week.md"
        if not base.exists():
            return base
        revision = 1
        while True:
            candidate = base.with_name(f"{base.stem}-r{revision}.md")
            if not candidate.exists():
                return candidate
            revision += 1

    def publish(
        self,
        *,
        task: LaneTask,
        draft: LaneDraft,
        approval: ApprovalGrant,
        harness,
    ) -> PublishResult:
        output_path = self._next_output_path()
        digest_bytes = draft.content.encode("utf-8")
        digest_hash = hashlib.sha256(digest_bytes).hexdigest()

        def write_local(_payload: dict[str, Any]) -> str:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                output_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o644,
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(digest_bytes)
            except Exception:
                output_path.unlink(missing_ok=True)
                raise
            return str(output_path)

        return harness.publish_with_ledger(
            task=task,
            external_target="local:file:tihna-digests",
            payload={
                "approval_token": approval.token,
                "final_file_path": str(output_path),
                "sha256": digest_hash,
            },
            side_effect_key=(
                f"lane:tihna:task:{task.id}:target:local-digest:v1"
            ),
            publisher=write_local,
        )

    def cleanup(self, *, task: LaneTask, harness) -> None:
        ranked = list(task.payload.get("ranked_items") or [])
        scores = [float(item.get("score") or 0) for item in ranked]
        outcome = str(task.payload.get("cleanup_outcome") or "")
        if outcome in {"published", "expired"}:
            for item in harness.list_tasks(status="ingested"):
                if item.payload.get("stage") == "item":
                    harness.update_task(task=item, status=outcome)
        values = {
            "digest_selected_count": len(
                [item for item in ranked if float(item.get("score") or 0) >= 60]
            ),
            "digest_total_items": len(ranked),
            "digest_avg_score": (
                sum(scores) / len(scores) if scores else 0.0
            ),
            "ingested_this_window": int(
                task.payload.get("ingested_this_window") or 0
            ),
        }
        for name, value in values.items():
            self._metric(harness, task, name, value)

    def run_stage(self, *, stage: str, harness):
        """Dispatch an operator CLI stage through the Tihna scheduler."""
        from hermes_cli.lanes.impls import tihna_scheduler

        if stage == "ingest":
            return tihna_scheduler.run_ingest(lane=self, harness=harness)
        if stage == "digest":
            if harness.llm_caller is None:
                harness.llm_caller = tihna_scheduler.default_llm_caller
            return tihna_scheduler.run_digest(lane=self, harness=harness)
        raise ValueError(f"unsupported Tihna scheduler stage: {stage}")


def build_lane() -> TihnaLane:
    return TihnaLane()


__all__ = ["TihnaLane", "build_lane"]
