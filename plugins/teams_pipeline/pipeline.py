"""Pipeline orchestration for Microsoft Teams meeting summaries."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx

from agent.secret_scope import get_secret

from agent.auxiliary_client import async_call_llm, extract_content_or_reasoning
from hermes_constants import get_hermes_home
from plugins.teams_pipeline.meetings import (
    download_recording_artifact,
    enrich_meeting_with_call_record,
    fetch_preferred_transcript_text,
    list_recording_artifacts,
    resolve_meeting_reference,
)
from plugins.teams_pipeline.models import (
    MeetingArtifact,
    SpeakerUpdate,
    StrategicDiscussion,
    StructuredActionItem,
    StructuredRisk,
    TeamsMeetingPipelineJob,
    TeamsMeetingRef,
    TeamsMeetingSummaryPayload,
)
from plugins.teams_pipeline.store import TeamsPipelineStore
from tools.transcription_tools import transcribe_audio

logger = logging.getLogger(__name__)

TERMINAL_PIPELINE_STATES = {"completed", "failed", "retry_scheduled"}
ACTIVE_PIPELINE_STATES = {
    "received",
    "resolving_meeting",
    "fetching_transcript",
    "downloading_recording",
    "transcribing_audio",
    "summarizing",
    "writing_notion",
    "writing_linear",
    "sending_teams",
    "pending_review",
}


class TeamsPipelineError(RuntimeError):
    """Base class for Teams meeting pipeline failures."""


class TeamsPipelineRetryableError(TeamsPipelineError):
    """Raised when the pipeline should be retried later."""


class TeamsPipelineSinkError(TeamsPipelineError):
    """Raised when an output sink fails."""


class TeamsPipelineArtifactNotFoundError(TeamsPipelineRetryableError):
    """Raised when meeting artifacts are not yet available."""


TranscribeFn = Callable[[str, Optional[str]], dict[str, Any]]
SummarizeFn = Callable[..., Awaitable[dict[str, Any] | TeamsMeetingSummaryPayload]]
SinkFn = Callable[
    [TeamsMeetingSummaryPayload, dict[str, Any], Optional[dict[str, Any]]],
    Awaitable[dict[str, Any]],
]
PendingReviewNotifier = Callable[[TeamsMeetingPipelineJob, TeamsMeetingSummaryPayload], Awaitable[None]]


@dataclass
class TeamsPipelineConfig:
    transcript_preferred: bool = True
    transcript_required: bool = False
    transcription_fallback: bool = True
    stt_model: str | None = None
    ffmpeg_extract_audio: bool = True
    transcript_min_chars: int = 80
    tmp_dir: Path | None = None
    notion: dict[str, Any] | None = None
    linear: dict[str, Any] | None = None
    teams_delivery: dict[str, Any] | None = None
    require_review: bool = False
    # Template-driven summarization (v1.0+)
    template_path: str | None = None
    llm_model: str | None = None

    @classmethod
    def from_dict(cls, payload: Optional[dict[str, Any]]) -> "TeamsPipelineConfig":
        data = dict(payload or {})
        tmp_dir = data.get("tmp_dir") or data.get("tmpDir")
        return cls(
            transcript_preferred=bool(data.get("transcript_preferred", True)),
            transcript_required=bool(data.get("transcript_required", False)),
            transcription_fallback=bool(data.get("transcription_fallback", True)),
            stt_model=data.get("stt_model") or data.get("sttModel"),
            ffmpeg_extract_audio=bool(data.get("ffmpeg_extract_audio", True)),
            transcript_min_chars=int(data.get("transcript_min_chars", 80)),
            tmp_dir=Path(tmp_dir) if tmp_dir else None,
            notion=data.get("notion"),
            linear=data.get("linear"),
            teams_delivery=data.get("teams_delivery") or data.get("teamsDelivery"),
            require_review=bool(data.get("require_review", False)),
            template_path=data.get("template_path") or data.get("templatePath"),
            llm_model=data.get("llm_model") or data.get("llmModel"),
        )


class NotionWriter:
    API_BASE = "https://api.notion.com/v1"
    API_VERSION = "2025-09-03"

    def __init__(self, *, api_key: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.api_key = (api_key or get_secret("NOTION_API_KEY", "") or "").strip()
        self._transport = transport

    async def write_summary(
        self,
        payload: TeamsMeetingSummaryPayload,
        config: dict[str, Any],
        existing_record: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise TeamsPipelineSinkError("NOTION_API_KEY is not configured.")

        database_id = str(config.get("database_id") or config.get("databaseId") or "").strip()
        page_id = (existing_record or {}).get("page_id")
        if not database_id and not page_id:
            raise TeamsPipelineSinkError("Notion sink requires database_id or an existing page_id.")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Notion-Version": self.API_VERSION,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=30.0, transport=self._transport) as client:
            if page_id:
                response = await client.patch(
                    f"{self.API_BASE}/pages/{page_id}",
                    headers=headers,
                    json={"properties": self._build_properties(payload, config)},
                )
                response.raise_for_status()
                record = response.json()
            else:
                response = await client.post(
                    f"{self.API_BASE}/pages",
                    headers=headers,
                    json={
                        "parent": {"database_id": database_id},
                        "properties": self._build_properties(payload, config),
                        "children": self._build_blocks(payload),
                    },
                )
                response.raise_for_status()
                record = response.json()

        return {"page_id": record["id"], "url": record.get("url")}

    def _build_properties(self, payload: TeamsMeetingSummaryPayload, config: dict[str, Any]) -> dict[str, Any]:
        title_property = config.get("title_property", "Name")
        summary_property = config.get("summary_property")
        meeting_id_property = config.get("meeting_id_property")

        properties: dict[str, Any] = {
            title_property: {
                "title": [{"text": {"content": payload.title or f"Meeting {payload.meeting_ref.meeting_id}"}}]
            }
        }
        if summary_property:
            properties[summary_property] = {
                "rich_text": [{"text": {"content": (payload.summary or "")[:1900]}}]
            }
        if meeting_id_property:
            properties[meeting_id_property] = {
                "rich_text": [{"text": {"content": payload.meeting_ref.meeting_id}}]
            }
        return properties

    def _build_blocks(self, payload: TeamsMeetingSummaryPayload) -> list[dict[str, Any]]:
        sections = [
            ("Summary", payload.summary or ""),
            ("Key Decisions", "\n".join(f"- {item}" for item in payload.key_decisions)),
            ("Action Items", "\n".join(f"- {item}" for item in payload.action_items)),
            ("Risks", "\n".join(f"- {item}" for item in payload.risks)),
        ]
        blocks: list[dict[str, Any]] = []
        for heading, body in sections:
            blocks.append(
                {
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {"rich_text": [{"text": {"content": heading}}]},
                }
            )
            blocks.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"text": {"content": body or "None"}}]},
                }
            )
        return blocks


class LinearWriter:
    API_URL = "https://api.linear.app/graphql"

    def __init__(self, *, api_key: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.api_key = (api_key or get_secret("LINEAR_API_KEY", "") or "").strip()
        self._transport = transport

    async def write_summary(
        self,
        payload: TeamsMeetingSummaryPayload,
        config: dict[str, Any],
        existing_record: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise TeamsPipelineSinkError("LINEAR_API_KEY is not configured.")

        headers = {"Authorization": self.api_key, "Content-Type": "application/json"}
        team_id = str(config.get("team_id") or config.get("teamId") or "").strip()
        title = payload.title or f"Meeting Summary: {payload.meeting_ref.meeting_id}"
        description = _render_summary_markdown(payload)
        existing_issue_id = (existing_record or {}).get("issue_id")

        async with httpx.AsyncClient(timeout=30.0, transport=self._transport) as client:
            if existing_issue_id:
                response = await client.post(
                    self.API_URL,
                    headers=headers,
                    json={
                        "query": (
                            "mutation($id: String!, $input: IssueUpdateInput!) "
                            "{ issueUpdate(id: $id, input: $input) { success issue { id identifier url } } }"
                        ),
                        "variables": {
                            "id": existing_issue_id,
                            "input": {"title": title, "description": description},
                        },
                    },
                )
            else:
                if not team_id:
                    raise TeamsPipelineSinkError("Linear sink requires team_id when creating a new issue.")
                response = await client.post(
                    self.API_URL,
                    headers=headers,
                    json={
                        "query": (
                            "mutation($input: IssueCreateInput!) "
                            "{ issueCreate(input: $input) { success issue { id identifier url } } }"
                        ),
                        "variables": {"input": {"teamId": team_id, "title": title, "description": description}},
                    },
                )
            response.raise_for_status()
            payload_json = response.json()

        issue = (
            (((payload_json.get("data") or {}).get("issueUpdate") or {}).get("issue"))
            or (((payload_json.get("data") or {}).get("issueCreate") or {}).get("issue"))
        )
        if not isinstance(issue, dict) or not issue.get("id"):
            raise TeamsPipelineSinkError(f"Linear write failed: {payload_json}")

        return {"issue_id": issue["id"], "identifier": issue.get("identifier"), "url": issue.get("url")}


class TeamsMeetingPipeline:
    """Transcript-first Teams meeting pipeline with durable lifecycle state."""

    def __init__(
        self,
        *,
        graph_client: Any,
        store: TeamsPipelineStore,
        config: TeamsPipelineConfig | dict[str, Any] | None = None,
        transcribe_fn: TranscribeFn = transcribe_audio,
        summarize_fn: Optional[SummarizeFn] = None,
        notion_writer: Optional[NotionWriter] = None,
        linear_writer: Optional[LinearWriter] = None,
        teams_sender: Optional[SinkFn] = None,
        pending_review_notifier: Optional[PendingReviewNotifier] = None,
    ) -> None:
        self.graph_client = graph_client
        self.store = store
        self.config = config if isinstance(config, TeamsPipelineConfig) else TeamsPipelineConfig.from_dict(config)
        self.transcribe_fn = transcribe_fn
        self.summarize_fn = summarize_fn or self._generate_summary_payload
        self.notion_writer = notion_writer
        self.linear_writer = linear_writer
        self.teams_sender = teams_sender
        self.pending_review_notifier = pending_review_notifier

    def create_job_from_notification(self, notification: dict[str, Any]) -> TeamsMeetingPipelineJob:
        event_id = TeamsPipelineStore.build_notification_receipt_key(notification)
        self.store.record_notification_receipt(event_id, notification)
        existing_job = self._find_job_by_dedupe_key(event_id)
        if existing_job is not None:
            return existing_job
        resource_data = notification.get("resourceData") or {}
        meeting_id = (
            resource_data.get("id")
            or notification.get("meetingId")
            or _extract_meeting_id_from_resource(str(notification.get("resource") or ""))
            or notification.get("resource")
            or event_id
        )
        job = TeamsMeetingPipelineJob(
            job_id=f"teams-job-{uuid.uuid4().hex[:12]}",
            event_id=event_id,
            source_event_type=str(notification.get("changeType") or "graph.notification"),
            dedupe_key=event_id,
            status="received",
            meeting_ref=TeamsMeetingRef(
                meeting_id=str(meeting_id),
                tenant_id=resource_data.get("tenantId") or notification.get("tenantId"),
                metadata={
                    "notification": dict(notification),
                    "join_web_url": resource_data.get("joinWebUrl"),
                    "call_record_id": resource_data.get("callRecordId") or notification.get("callRecordId"),
                },
            ),
        )
        self.store.upsert_job(job.job_id, job.to_dict())
        return job

    async def run_notification(self, notification: dict[str, Any]) -> TeamsMeetingPipelineJob:
        job = self.create_job_from_notification(notification)
        if job.status in TERMINAL_PIPELINE_STATES:
            return job
        if job.status in ACTIVE_PIPELINE_STATES - {"received"}:
            # Stale-job recovery: if the job has been stuck in an active state
            # for more than STALE_JOB_MINUTES, reset it to "received" so the
            # full pipeline re-runs on the next tick.
            stale_minutes = int(os.getenv("TEAMS_PIPELINE_STALE_JOB_MINUTES", "10"))
            updated_at = job.updated_at
            if isinstance(updated_at, str):
                try:
                    updated_at = datetime.fromisoformat(updated_at)
                except (ValueError, TypeError):
                    updated_at = None
            if updated_at is not None:
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - updated_at).total_seconds() / 60
                if age > stale_minutes:
                    logger.warning(
                        "Teams pipeline job %s stuck in %s for %.0f min — resetting to received",
                        job.job_id, job.status, age,
                    )
                    self.store.upsert_job(job.job_id, {**job.to_dict(), "status": "received"})
                    return await self.run_job(job.job_id)
            return job
        return await self.run_job(job.job_id)

    async def run_job(self, job_or_id: TeamsMeetingPipelineJob | str) -> TeamsMeetingPipelineJob:
        job = self._coerce_job(job_or_id)
        meeting_ref = job.meeting_ref
        if meeting_ref is None:
            raise TeamsPipelineError(f"Job {job.job_id} has no meeting_ref.")

        artifacts: list[MeetingArtifact] = []

        try:
            job = self._persist_job(job, status="resolving_meeting")
            notification = (meeting_ref.metadata or {}).get("notification") or {}

            # Skip re-resolution if meeting_ref already has full metadata
            # (e.g., resolved from recap URL with call record data)
            md = meeting_ref.metadata or {}
            already_resolved = bool(md.get("subject") and md.get("startDateTime") and md.get("endDateTime"))
            if already_resolved:
                resolved_meeting = meeting_ref
                logger.info("Meeting already resolved via recap URL: %s", resolved_meeting.meeting_id[:40])
            else:
                resolved_meeting = await resolve_meeting_reference(
                    self.graph_client,
                    meeting_id=meeting_ref.meeting_id,
                    join_web_url=meeting_ref.join_web_url or meeting_ref.metadata.get("join_web_url"),
                    tenant_id=meeting_ref.tenant_id,
                )
                job.meeting_ref = resolved_meeting
                job = self._persist_job(job, meeting_ref=resolved_meeting.to_dict())

            transcript_text: str | None = None
            if self.config.transcript_preferred:
                job = self._persist_job(job, status="fetching_transcript")
                transcript_artifact, transcript_text = await fetch_preferred_transcript_text(
                    self.graph_client, resolved_meeting
                )
                if transcript_artifact and transcript_text:
                    artifacts.append(transcript_artifact)
                    if len(transcript_text.strip()) < self.config.transcript_min_chars:
                        transcript_text = None

            if not transcript_text:
                if self.config.transcript_required:
                    raise TeamsPipelineRetryableError(
                        f"Transcript unavailable for meeting {resolved_meeting.meeting_id}."
                    )
                if not self.config.transcription_fallback:
                    raise TeamsPipelineArtifactNotFoundError(
                        "No transcript available and transcription fallback disabled "
                        f"for {resolved_meeting.meeting_id}."
                    )
                job = self._persist_job(job, status="downloading_recording")
                recordings = await list_recording_artifacts(self.graph_client, resolved_meeting)
                if not recordings:
                    raise TeamsPipelineRetryableError(
                        f"Recording unavailable for meeting {resolved_meeting.meeting_id}."
                    )
                recording = recordings[0]
                artifacts.append(recording)
                transcript_text = await self._transcribe_recording(job, resolved_meeting, recording)
                job = self._persist_job(job, selected_artifact_strategy="recording_stt_fallback")
            else:
                job = self._persist_job(job, selected_artifact_strategy="transcript_first")

            call_record_id = notification.get("callRecordId") or (meeting_ref.metadata or {}).get("call_record_id")
            call_record = await enrich_meeting_with_call_record(
                self.graph_client,
                resolved_meeting,
                call_record_id=call_record_id,
            )
            if call_record is not None:
                artifacts.append(call_record)

            job = self._persist_job(job, status="summarizing")
            generated = await asyncio.wait_for(
                self.summarize_fn(
                    resolved_meeting=resolved_meeting,
                    transcript_text=transcript_text or "",
                    artifacts=artifacts,
                ),
                timeout=180.0,
            )
            summary_payload = (
                generated
                if isinstance(generated, TeamsMeetingSummaryPayload)
                else TeamsMeetingSummaryPayload.from_dict(generated)
            )
            job.summary_payload = summary_payload
            job = self._persist_job(job, summary_payload=summary_payload.to_dict())


            if self.config.require_review:
                job = self._persist_job(job, status="pending_review")
                await self._notify_pending_review(job, summary_payload)
            else:
                await self._write_sinks(job, summary_payload)
                job = self._persist_job(job, status="completed")
            return job

        except TeamsPipelineRetryableError as exc:
            job = self._persist_job(
                job,
                status="retry_scheduled",
                error_info={"message": str(exc), "retryable": True},
            )
            return job
        except Exception as exc:
            job = self._persist_job(
                job,
                status="failed",
                error_info={"message": str(exc), "type": type(exc).__name__},
            )
            return job

    def _coerce_job(self, job_or_id: TeamsMeetingPipelineJob | str) -> TeamsMeetingPipelineJob:
        if isinstance(job_or_id, TeamsMeetingPipelineJob):
            return job_or_id
        payload = self.store.get_job(str(job_or_id))
        if not payload:
            raise TeamsPipelineError(f"Unknown Teams pipeline job: {job_or_id}")
        return TeamsMeetingPipelineJob.from_dict(payload)

    def _find_job_by_dedupe_key(self, dedupe_key: str) -> TeamsMeetingPipelineJob | None:
        for payload in self.store.list_jobs().values():
            if not isinstance(payload, dict):
                continue
            if str(payload.get("dedupe_key") or "") != dedupe_key:
                continue
            return TeamsMeetingPipelineJob.from_dict(payload)
        return None

    def _persist_job(self, job: TeamsMeetingPipelineJob, **updates: Any) -> TeamsMeetingPipelineJob:
        payload = job.to_dict()
        payload.update(updates)
        stored = self.store.upsert_job(job.job_id, payload)
        return TeamsMeetingPipelineJob.from_dict(stored)

    async def _transcribe_recording(
        self,
        job: TeamsMeetingPipelineJob,
        meeting_ref: TeamsMeetingRef,
        recording: MeetingArtifact,
    ) -> str:
        temp_root = self.config.tmp_dir or (get_hermes_home() / "tmp" / "teams_pipeline")
        temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(temp_root), prefix="teams-recording-") as tmp_dir:
            # display_name comes from Graph API and is ultimately set by
            # the meeting organizer — strip any directory components so a
            # crafted name like "../../etc/cron.d/evil" can't escape tmp_dir.
            # Path(...).name reduces "." / ".." / "" to themselves, so the
            # dot-only basenames must be rejected explicitly (joining "tmp/.."
            # resolves to the parent dir); fall back to the artifact id.
            fallback_name = f"{recording.artifact_id}.mp4"
            raw_name = recording.display_name or fallback_name
            recording_name = Path(raw_name).name
            if recording_name in ("", ".", ".."):
                recording_name = fallback_name
            recording_path = Path(tmp_dir) / recording_name
            await asyncio.wait_for(
                download_recording_artifact(
                    self.graph_client,
                    meeting_ref,
                    recording,
                    recording_path,
                ),
                timeout=300,  # 5-minute timeout — large recordings can be slow
            )
            audio_path = await self._prepare_audio_path(recording_path)
            job = self._persist_job(job, status="transcribing_audio")
            result = await asyncio.to_thread(self.transcribe_fn, str(audio_path), self.config.stt_model)
            if not result.get("success"):
                raise TeamsPipelineRetryableError(str(result.get("error") or "Unknown STT failure"))
            transcript = str(result.get("transcript") or "").strip()
            if not transcript:
                raise TeamsPipelineRetryableError("STT returned an empty transcript.")
            return transcript

    async def _prepare_audio_path(self, recording_path: Path) -> Path:
        if recording_path.suffix.lower() in {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".aac", ".webm"}:
            return recording_path
        if not self.config.ffmpeg_extract_audio:
            return recording_path
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise TeamsPipelineRetryableError(
                "Recording fallback requires ffmpeg for audio extraction, but ffmpeg was not found."
            )
        audio_path = recording_path.with_suffix(".wav")
        proc = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-y",
            "-i",
            str(recording_path),
            str(audio_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise TeamsPipelineRetryableError(f"ffmpeg audio extraction failed: {detail}")
        return audio_path

    async def _generate_summary_payload(
        self,
        *,
        resolved_meeting: TeamsMeetingRef,
        transcript_text: str,
        artifacts: list[MeetingArtifact],
    ) -> TeamsMeetingSummaryPayload:
        # Attempt template-driven summarization (v1.0+)
        template = _load_summary_template(self.config.template_path)
        parsed: dict[str, Any] = {}
        if template is not None:
            try:
                system_prompt = _build_system_prompt_from_template(template)
                user_prompt = _build_summary_prompt(
                    resolved_meeting, transcript_text, artifacts,
                    template=template,
                )
                llm_kwargs: dict[str, Any] = {
                    "task": "call",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.2,
                    "max_tokens": 4096,
                }
                if self.config.llm_model:
                    llm_kwargs["model"] = self.config.llm_model
                response = await async_call_llm(**llm_kwargs)
                content = extract_content_or_reasoning(response)
                parsed = _parse_summary_json(content, template=template)
            except Exception as exc:
                logger.warning(
                    "Teams pipeline: template-driven summarization failed (%s), falling back to hardcoded behavior", exc,
                )
                template = None
                parsed = {}

        # Hardcoded fallback (original behavior)
        if template is None and not parsed:
            prompt = _build_summary_prompt(resolved_meeting, transcript_text, artifacts)
            try:
                response = await async_call_llm(
                    task="call",
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You summarize meeting transcripts. Return only valid JSON with keys: "
                                "summary, key_decisions, action_items, risks, confidence, confidence_notes."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.2,
                    max_tokens=900,
                )
                content = extract_content_or_reasoning(response)
                parsed = _parse_summary_json(content)
            except Exception as exc:
                logger.info("Teams pipeline LLM summary unavailable, using heuristic summary: %s", exc)
                parsed = _heuristic_summary(transcript_text)

        # Build structured risks from Stage 1 parsed data (risks are simple enough for Stage 1)
        structured_risks = parsed.get("structured_risks", [])
        if not structured_risks and parsed.get("risks"):
            structured_risks = [
                StructuredRisk(risk=str(item), impact="Not assessed")
                for item in parsed["risks"]
                if str(item).strip()
            ]

        metrics = _collect_call_metrics(artifacts)
        participants = _collect_participants(resolved_meeting)
        payload = TeamsMeetingSummaryPayload(
            meeting_ref=resolved_meeting,
            title=str(resolved_meeting.metadata.get("subject") or f"Meeting {resolved_meeting.meeting_id}"),
            start_time=resolved_meeting.metadata.get("startDateTime"),
            end_time=resolved_meeting.metadata.get("endDateTime"),
            participants=participants,
            transcript_text=transcript_text,
            summary=parsed.get("summary"),
            key_decisions=list(parsed.get("key_decisions") or []),
            action_items=list(parsed.get("action_items") or []),
            risks=list(parsed.get("risks") or []),
            call_metrics=metrics,
            source_artifacts=artifacts,
            confidence=parsed.get("confidence"),
            confidence_notes=parsed.get("confidence_notes"),
            notion_target=(self.config.notion or {}).get("database_id"),
            linear_target=(self.config.linear or {}).get("team_id"),
            teams_target=(
                (self.config.teams_delivery or {}).get("channel_id")
                or (self.config.teams_delivery or {}).get("chat_id")
            ),
            structured_risks=structured_risks,
        )

        # ── Stage 2: Focused Structured Extraction ──────────────
        # Only runs when template was loaded successfully.
        # Stage 2 extracts: speakers[], structured_action_items, decision fields, strategic_discussions.
        # On failure: graceful degradation (Stage 1 output + workarounds + VTT fallback).
        stage2_ok = False
        if template is not None:
            try:
                stage2_system = _build_stage2_system_prompt(template)
                # Build transcript excerpt (last N chars for recency bias)
                excerpt = transcript_text[-STAGE2_TRANSCRIPT_EXCERPT_CHARS:] if len(transcript_text) > STAGE2_TRANSCRIPT_EXCERPT_CHARS else transcript_text
                stage2_user = _build_stage2_user_prompt(
                    stage1_summary=payload.summary or "",
                    transcript_excerpt=excerpt,
                    speaker_list=participants,
                )
                stage2_kwargs: dict[str, Any] = {
                    "task": "call",
                    "messages": [
                        {"role": "system", "content": stage2_system},
                        {"role": "user", "content": stage2_user},
                    ],
                    "temperature": 0.2,
                    "max_tokens": 2048,
                }
                if self.config.llm_model:
                    stage2_kwargs["model"] = self.config.llm_model
                stage2_response = await async_call_llm(**stage2_kwargs)
                stage2_content = extract_content_or_reasoning(stage2_response)
                stage2_parsed = _parse_stage2_json(stage2_content)

                if stage2_parsed:
                    # Merge Stage 2 output into payload
                    if stage2_parsed.get("speakers"):
                        payload.speakers = stage2_parsed["speakers"]
                    if stage2_parsed.get("structured_action_items"):
                        payload.structured_action_items = stage2_parsed["structured_action_items"]
                    if stage2_parsed.get("decision_headline"):
                        payload.decision_headline = stage2_parsed["decision_headline"]
                    if stage2_parsed.get("decision_chosen"):
                        payload.decision_chosen = stage2_parsed["decision_chosen"]
                    if stage2_parsed.get("decision_rejected"):
                        payload.decision_rejected = stage2_parsed["decision_rejected"]
                    if stage2_parsed.get("strategic_discussions"):
                        payload.strategic_discussions = stage2_parsed["strategic_discussions"]
                    stage2_ok = True
                    logger.info(
                        "Teams pipeline Stage 2: extracted %d speakers, %d action items",
                        len(payload.speakers), len(payload.structured_action_items),
                    )
            except Exception as exc:
                logger.warning("Teams pipeline Stage 2: failed (%s), falling back to workarounds", exc)

        # ── Stage 2 fallback: workarounds when Stage 2 failed ────
        if template is not None and not stage2_ok:
            # Fallback 1: extract "Name: action" from flat action_items
            import re as _re
            fallback_actions = []
            for item in (parsed.get("action_items") or []):
                item_str = str(item).strip()
                if not item_str:
                    continue
                m = _re.match(r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s*[:\-]\s*(.+)$", item_str)
                if m:
                    fallback_actions.append(StructuredActionItem(who=m.group(1).strip(), action=m.group(2).strip()))
                else:
                    fallback_actions.append(StructuredActionItem(who="Unassigned", action=item_str))
            payload.structured_action_items = fallback_actions

            # Fallback 2: VTT speaker extraction (names + counts only, no semantic bullets)
            if not payload.speakers and transcript_text:
                payload.speakers = _extract_speakers_from_vtt(transcript_text)
                logger.info("Teams pipeline: VTT fallback extracted %d speakers", len(payload.speakers))

        # Pre-render HTML for Teams sink when template was loaded successfully
        if template is not None:
            payload.rendered_html = _render_summary_html(payload, template)

        return payload

    async def _write_sinks(self, job: TeamsMeetingPipelineJob, payload: TeamsMeetingSummaryPayload) -> None:
        if self.config.notion and self.config.notion.get("enabled") and self.notion_writer:
            try:
                job = self._persist_job(job, status="writing_notion")
                sink_key = f"notion:{payload.meeting_ref.meeting_id}"
                existing = self.store.get_sink_record(sink_key)
                result = await self.notion_writer.write_summary(payload, self.config.notion, existing)
                self.store.upsert_sink_record(sink_key, result)
            except Exception as exc:
                logger.warning("Teams pipeline Notion sink failed: %s", exc)
                self.store.upsert_sink_record(f"error:notion:{payload.meeting_ref.meeting_id}", {"error": str(exc)})

        if self.config.linear and self.config.linear.get("enabled") and self.linear_writer:
            try:
                job = self._persist_job(job, status="writing_linear")
                sink_key = f"linear:{payload.meeting_ref.meeting_id}"
                existing = self.store.get_sink_record(sink_key)
                result = await self.linear_writer.write_summary(payload, self.config.linear, existing)
                self.store.upsert_sink_record(sink_key, result)
            except Exception as exc:
                logger.warning("Teams pipeline Linear sink failed: %s", exc)
                self.store.upsert_sink_record(f"error:linear:{payload.meeting_ref.meeting_id}", {"error": str(exc)})

        if self.config.teams_delivery and self.config.teams_delivery.get("enabled") and self.teams_sender:
            try:
                job = self._persist_job(job, status="sending_teams")
                sink_key = f"teams:{payload.meeting_ref.meeting_id}"
                existing = self.store.get_sink_record(sink_key)
                if hasattr(self.teams_sender, "write_summary"):
                    result = await self.teams_sender.write_summary(payload, self.config.teams_delivery, existing)
                else:
                    result = await self.teams_sender(payload, self.config.teams_delivery, existing)
                self.store.upsert_sink_record(sink_key, result)
            except Exception as exc:
                logger.warning("Teams pipeline Teams sink failed: %s", exc)
                self.store.upsert_sink_record(f"error:teams:{payload.meeting_ref.meeting_id}", {"error": str(exc)})

    async def _notify_pending_review(self, job: TeamsMeetingPipelineJob, payload: TeamsMeetingSummaryPayload) -> None:
        logger.info("Meeting %s is pending review. Job ID: %s", payload.meeting_ref.meeting_id, job.job_id)
        if self.pending_review_notifier is None:
            self.store.upsert_sink_record(
                f"pending_review:{job.job_id}",
                {
                    "job_id": job.job_id,
                    "meeting_id": payload.meeting_ref.meeting_id,
                    "title": payload.title,
                    "message": _build_pending_review_message(job, payload),
                    "delivered": False,
                },
            )
            return

        try:
            await self.pending_review_notifier(job, payload)
        except Exception as exc:
            logger.warning("Teams pipeline pending-review notification failed: %s", exc)
            self.store.upsert_sink_record(
                f"error:pending_review:{job.job_id}",
                {"error": str(exc), "job_id": job.job_id},
            )


def _collect_call_metrics(artifacts: list[MeetingArtifact]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for artifact in artifacts:
        if artifact.artifact_type == "call_record":
            metrics.update(dict(artifact.metadata.get("metrics") or {}))
    metrics["artifact_count"] = len(artifacts)
    return metrics


def _collect_participants(meeting_ref: TeamsMeetingRef) -> list[str]:
    participants = meeting_ref.metadata.get("participants")
    if not participants:
        return []

    result: list[str] = []

    def _extract_name(person: dict[str, Any]) -> str | None:
        """Extract display name, fallback to UPN."""
        if not isinstance(person, dict):
            return None
        name = person.get("displayName")
        if name:
            return str(name)
        # Fallback: nested identity.user.displayName
        user = (person.get("identity") or {}).get("user") or {}
        name = user.get("displayName")
        if name:
            return str(name)
        # Final fallback: UPN
        upn = person.get("upn")
        if upn:
            return str(upn)
        return None

    # Format 1: nested dict with organizer + attendees (call record format)
    if isinstance(participants, dict):
        organizer = participants.get("organizer")
        if organizer:
            name = _extract_name(organizer)
            if name:
                result.append(name)
        attendees = participants.get("attendees") or []
        if isinstance(attendees, list):
            for person in attendees:
                name = _extract_name(person)
                if name:
                    result.append(name)
    # Format 2: flat list (legacy format)
    elif isinstance(participants, list):
        for item in participants:
            if isinstance(item, dict):
                name = _extract_name(item)
                if name:
                    result.append(str(name))

    return result


def _extract_meeting_id_from_resource(resource: str) -> str | None:
    if not resource:
        return None
    parts = [part for part in resource.split("/") if part]
    if not parts:
        return None
    if "onlineMeetings" in parts:
        index = parts.index("onlineMeetings")
        if index + 1 < len(parts):
            return parts[index + 1]
    return parts[-1]


def _build_summary_prompt(
    meeting_ref: TeamsMeetingRef,
    transcript_text: str,
    artifacts: list[MeetingArtifact],
    template: dict[str, Any] | None = None,
) -> str:
    artifact_lines = [f"- {artifact.artifact_type}:{artifact.artifact_id}:{artifact.display_name or ''}" for artifact in artifacts]
    transcript_limit = TRANSCRIPT_MAX_CHARS if template is not None else 18000
    transcript_excerpt = transcript_text[:transcript_limit]
    truncation_note = "\n[Transcript truncated — last portion omitted]" if len(transcript_text) > transcript_limit else ""

    lines = [
        f"Meeting ID: {meeting_ref.meeting_id}",
        f"Title: {meeting_ref.metadata.get('subject') or 'Unknown'}",
        f"Artifacts:\n{chr(10).join(artifact_lines) or '- none'}",
    ]

    # Speaker list (template-driven)
    if template is not None:
        participants = _collect_participants(meeting_ref)
        if participants:
            lines.append(f"\nAttendees: {', '.join(participants)}")

    lines.append(f"\nTranscript:\n{transcript_excerpt}{truncation_note}")
    return "\n".join(lines)


def _parse_summary_json(content: str, template: dict[str, Any] | None = None) -> dict[str, Any]:
    text = (content or "").strip()
    if not text:
        return _heuristic_summary("")
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _heuristic_summary(content)

    # Parse speakers (new format)
    speakers = []
    if template is not None:
        for item in payload.get("speakers") or []:
            if isinstance(item, dict) and item.get("name"):
                speakers.append(SpeakerUpdate.from_dict(item))

    # Parse strategic discussions (new format)
    strategic_discussions = []
    if template is not None:
        for item in (payload.get("strategic_discussions") or payload.get("strategicDiscussions") or []):
            if isinstance(item, dict) and item.get("topic"):
                strategic_discussions.append(StrategicDiscussion.from_dict(item))

    # Parse action items: auto-detect new {who, action} dicts vs old strings
    action_items_raw = payload.get("action_items") or []
    action_items: list[str] = []
    structured_action_items: list[StructuredActionItem] = []
    if template is not None and action_items_raw:
        for item in action_items_raw:
            if isinstance(item, dict) and "who" in item and "action" in item:
                structured_action_items.append(StructuredActionItem.from_dict(item))
                action_items.append(f"{item['who']}: {item['action']}")
            else:
                action_items.append(str(item).strip())
    else:
        action_items = [str(item).strip() for item in action_items_raw if str(item).strip()]

    # Parse risks: auto-detect new {risk, impact} dicts vs old strings
    risks_raw = payload.get("risks") or []
    risks: list[str] = []
    structured_risks: list[StructuredRisk] = []
    if template is not None and risks_raw:
        for item in risks_raw:
            if isinstance(item, dict) and "risk" in item:
                structured_risks.append(StructuredRisk.from_dict(item))
                risks.append(f"{item['risk']} — {item.get('impact', 'Not assessed')}")
            else:
                risks.append(str(item).strip())
    else:
        risks = [str(item).strip() for item in risks_raw if str(item).strip()]

    result = {
        "summary": str(payload.get("summary") or "").strip(),
        "key_decisions": [str(item).strip() for item in payload.get("key_decisions", []) if str(item).strip()],
        "action_items": action_items,
        "risks": risks,
        "confidence": str(payload.get("confidence") or "medium").strip(),
        "confidence_notes": str(payload.get("confidence_notes") or "").strip(),
        # Extended fields (template v1.0+)
        "speakers": speakers,
        "strategic_discussions": strategic_discussions,
        "decision_headline": payload.get("decision_headline"),
        "decision_chosen": payload.get("decision_chosen"),
        "decision_rejected": payload.get("decision_rejected"),
        "structured_action_items": structured_action_items,
        "structured_risks": structured_risks,
    }
    return result


def _heuristic_summary(transcript_text: str) -> dict[str, Any]:
    lines = [line.strip(" -*\t") for line in transcript_text.splitlines() if line.strip()]
    summary = " ".join(lines[:3])[:1200] or "Transcript unavailable or too sparse for a confident summary."
    action_items = [
        line for line in lines if line.lower().startswith(("action:", "todo:", "next step:", "follow up:"))
    ][:8]
    risks = [line for line in lines if "risk" in line.lower() or "blocker" in line.lower()][:6]
    decisions = [line for line in lines if "decide" in line.lower() or "decision" in line.lower()][:6]
    confidence = "low" if len(transcript_text.strip()) < 300 else "medium"
    return {
        "summary": summary,
        "key_decisions": decisions,
        "action_items": action_items,
        "risks": risks,
        "confidence": confidence,
        "confidence_notes": "Generated with heuristic fallback because no LLM summary response was available.",
    }


# ─── Template-driven summarization (v1.0+) ─────────────────────────

DEFAULT_TEMPLATE_PATH = str(
    Path(__file__).resolve().parents[2]
    / "skills"
    / "templates"
    / "hermes-agent"
    / "team-pipeline"
    / "summary-template.yaml"
)
KNOWN_TEMPLATE_VERSIONS = ("1.0", "2.0")
TRANSCRIPT_MAX_CHARS = 60_000


def _build_pending_review_message(job: TeamsMeetingPipelineJob, payload: TeamsMeetingSummaryPayload) -> str:
    return (
        "Teams Meeting Summary Ready for Review\n\n"
        f"Title: {payload.title or 'Meeting'}\n"
        f"Job ID: `{job.job_id}`\n\n"
        f"Run `hermes teams-pipeline show {job.job_id}` to view details.\n"
        f"Run `hermes teams-pipeline deliver {job.job_id}` to approve and deliver to Teams."
    )


def _load_summary_template(template_path: str | None = None) -> dict[str, Any] | None:
    """Load and validate the summary template YAML. Returns None on failure (file missing, parse error, unknown version)."""
    path = template_path or DEFAULT_TEMPLATE_PATH
    if yaml is None:
        logger.warning("Teams pipeline: PyYAML not installed; cannot load template from %s", path)
        return None
    try:
        with open(path, "r") as f:
            template = yaml.safe_load(f)
    except FileNotFoundError:
        logger.warning("Teams pipeline: summary template not found at %s — falling back to hardcoded behavior", path)
        return None
    except Exception as exc:
        logger.warning("Teams pipeline: failed to parse summary template at %s: %s", path, exc)
        return None

    version = (template.get("meta") or {}).get("version")
    if version not in KNOWN_TEMPLATE_VERSIONS:
        logger.warning(
            "Teams pipeline: unknown template version %r at %s (known: %s) — falling back to hardcoded behavior",
            version, path, KNOWN_TEMPLATE_VERSIONS,
        )
        return None

    return template


def _build_system_prompt_from_template(template: dict[str, Any]) -> str:
    """Construct the LLM system prompt from template rules, anti-patterns, quality checklist, and style."""
    sections = template.get("sections", [])
    style = template.get("style", {})
    quality = template.get("quality_checklist", [])
    anti = template.get("anti_patterns", [])

    parts = ["You are a meeting summarization agent. Generate a structured summary following these rules:\n"]

    # Section rules
    for section in sections:
        parts.append(f"\n## {section['id']}\n")
        for rule in section.get("rules", []):
            parts.append(f"- {rule}")

    # Style rules
    if style:
        parts.append("\n## Style\n")
        if style.get("bold_speaker_names"):
            parts.append("- Speaker names MUST be bold")
        if style.get("code_inline"):
            parts.append("- Use <code> tags for code references")
        if style.get("section_separator"):
            parts.append(f"- Use {style['section_separator']} between sections")
        bullets_range = style.get("bullets_per_speaker")
        if bullets_range:
            parts.append(f"- Aim for {bullets_range} bullets per speaker")

    # Quality checklist
    if quality:
        parts.append("\n## Quality Checklist\n")
        for item in quality:
            parts.append(f"- {item}")

    # Anti-patterns
    if anti:
        parts.append("\n## Anti-Patterns (DO NOT)\n")
        for item in anti:
            parts.append(f"- {item}")

    # Extended JSON schema specification — Stage 1 only requests overview fields.
    # Structured extraction (speakers, action_items who/action, decisions) is handled by Stage 2.
    parts.append(
        "\n## Output Format\n"
        "Return ONLY valid JSON (no markdown code fences). The JSON must match this schema:\n"
        "{\n"
        '  "summary": str,\n'
        '  "key_decisions": [str],\n'
        '  "action_items": [str],\n'
        '  "risks": [str],\n'
        '  "confidence": str,\n'
        '  "confidence_notes": str\n'
        "}\n"
        "- **summary**: 3-5 sentence overview of the meeting\n"
        "- **key_decisions**: List of important decisions made (as strings)\n"
        "- **action_items**: List of action items (as strings, include the person's name in each item)\n"
        "- **risks**: List of identified risks (as strings)\n"
        "- **confidence**: 'high', 'medium', or 'low'\n"
        "- **confidence_notes**: Brief explanation of confidence level\n"
    )

    return "\n".join(parts)


# ─── Stage 2: Focused Structured Extraction (v1.0+) ──────────────

STAGE2_TRANSCRIPT_EXCERPT_CHARS = 10_000


def _build_stage2_system_prompt(template: dict[str, Any]) -> str:
    """Build a narrow system prompt focused ONLY on structured extraction.

    Stage 2 does NOT generate overview summaries — it extracts structured data
    from the transcript using Stage 1's summary as context. Narrower prompt =
    higher LLM schema compliance.
    """
    parts = [
        "You are a meeting data extraction agent. Your ONLY job is to extract structured data from the transcript.\n"
        "You are given: (1) a summary already generated by another agent, (2) a transcript excerpt, (3) a speaker list.\n"
        "Return ONLY valid JSON (no markdown code fences, no explanation).\n",
        "\n## Required Output Schema\n"
        "{\n"
        '  "speakers": [\n'
        '    {"name": str, "topic_label": str, "bullets": [str, str, str]}\n'
        "  ],\n"
        '  "structured_action_items": [\n'
        '    {"who": str, "action": str}\n'
        "  ],\n"
        '  "decision_headline": str or null,\n'
        '  "decision_chosen": str or null,\n'
        '  "decision_rejected": str or null,\n'
        '  "strategic_discussions": [\n'
        '    {"topic": str, "content": str}\n'
        "  ]\n"
        "}\n",
        "\n## Field Rules\n"
        "- **speakers[]**: One entry per person who spoke 3+ times. Include their FIRST name only.\n"
        "  - topic_label: 3-5 word summary of what they discussed (e.g., 'Database Schema Refactor')\n"
        "  - bullets: 3-7 concrete facts per speaker — code references, file paths, class names, specific decisions, quotes\n"
        "  - Each bullet is ONE fact, not a paragraph\n"
        "- **structured_action_items[]**: Each item has 'who' (person's FIRST name only, e.g., 'Phuong' not 'Phuong Lambert') "
        "and 'action' (the task description starting with a verb). Do NOT embed the name in the action text.\n"
        "- **decision_headline**: One-line headline if a clear decision was made, otherwise null\n"
        "- **decision_chosen**: Brief description of the chosen approach, otherwise null\n"
        "- **decision_rejected**: Brief description of rejected alternatives, otherwise null\n"
        "- **strategic_discussions[]**: Non-status strategic topics discussed (product vision, cross-team concerns), otherwise empty array\n",
    ]
    return "\n".join(parts)


def _build_stage2_user_prompt(
    stage1_summary: str,
    transcript_excerpt: str,
    speaker_list: list[str],
) -> str:
    """Build the user prompt for Stage 2 structured extraction.

    Input: Stage 1 summary (context), last 10K chars of transcript (recency bias),
    and the speaker list from meeting metadata.
    """
    parts = []
    parts.append("## Stage 1 Summary (for context)\n")
    parts.append(stage1_summary or "(No summary available)")
    parts.append("\n## Transcript Excerpt (last portion)\n")
    parts.append(f"```\n{transcript_excerpt}\n```")
    if speaker_list:
        parts.append("\n## Speaker List\n")
        parts.append(", ".join(speaker_list))
    parts.append(
        "\n## Instructions\n"
        "Extract structured data from the transcript above. "
        "For each speaker who spoke 3+ times, provide their name, a topic label, and 3-7 bullet points. "
        "Extract action items with proper who/action split. "
        "Identify any key decision and strategic discussions. "
        "Return ONLY valid JSON matching the schema in the system prompt."
    )
    return "\n".join(parts)


def _parse_stage2_json(content: str) -> dict[str, Any]:
    """Parse Stage 2 LLM response into structured fields.

    Returns empty dict on failure with WARNING log. Lenient — missing keys
    get default values rather than causing errors.
    """
    if not content or not content.strip():
        logger.warning("Teams pipeline Stage 2: empty LLM response")
        return {}

    # Strip markdown code fences if present
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines if they are fences
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Teams pipeline Stage 2: JSON parse failed (%s), returning empty", exc)
        return {}

    if not isinstance(data, dict):
        logger.warning("Teams pipeline Stage 2: response is not a JSON object, returning empty")
        return {}

    # Convert raw dicts to dataclass instances
    speakers_raw = data.get("speakers") or []
    speakers = []
    for s in speakers_raw:
        if isinstance(s, dict) and s.get("name"):
            speakers.append(SpeakerUpdate(
                name=str(s["name"]).strip(),
                topic_label=str(s.get("topic_label") or "Update").strip(),
                bullets=list(s.get("bullets") or []),
            ))

    action_items_raw = data.get("structured_action_items") or []
    action_items = []
    for ai in action_items_raw:
        if isinstance(ai, dict) and ai.get("who") and ai.get("action"):
            action_items.append(StructuredActionItem(
                who=str(ai["who"]).strip(),
                action=str(ai["action"]).strip(),
            ))

    discussions_raw = data.get("strategic_discussions") or []
    discussions = []
    for d in discussions_raw:
        if isinstance(d, dict) and d.get("topic"):
            discussions.append(StrategicDiscussion(
                topic=str(d["topic"]).strip(),
                content=str(d.get("content") or "").strip(),
            ))

    return {
        "speakers": speakers,
        "structured_action_items": action_items,
        "decision_headline": data.get("decision_headline"),
        "decision_chosen": data.get("decision_chosen"),
        "decision_rejected": data.get("decision_rejected"),
        "strategic_discussions": discussions,
    }


def _extract_speakers_from_vtt(transcript: str) -> list[SpeakerUpdate]:
    """Extract unique speakers from WEBVTT transcript and create basic SpeakerUpdate entries.

    Parses <v Speaker Name> tags and counts utterances per speaker.
    Only includes speakers with 3+ utterances (filters out brief interjections).
    """
    import re as _re
    speaker_utterances: dict[str, int] = {}
    # Match VTT voice tags: <v Speaker Name>text</v>
    for match in _re.finditer(r"<v\s+([^>]+)>", transcript):
        name = match.group(1).strip()
        if name:
            speaker_utterances[name] = speaker_utterances.get(name, 0) + 1

    # Only include speakers with 3+ utterances (filter brief interjections)
    speakers = []
    for name, count in sorted(speaker_utterances.items(), key=lambda x: -x[1]):
        if count >= 3:
            speakers.append(SpeakerUpdate(
                name=name,
                topic_label="Update",
                bullets=[f"Spoke {count} times during the meeting (detailed bullets pending LLM generation)"],
            ))

    return speakers


def _render_summary_markdown(payload: TeamsMeetingSummaryPayload) -> str:
    lines = [
        f"# {payload.title or f'Meeting {payload.meeting_ref.meeting_id}'}",
        "",
    ]

    # ── Status Updates (per-speaker) ────────────────────────
    if payload.speakers:
        lines.append("## Status Updates & Code Exploration")
        lines.append("")
        for speaker in payload.speakers:
            topic = speaker.topic_label or ""
            label = f"**{speaker.name} — {topic}**" if topic else f"**{speaker.name}**"
            lines.append(label)
            for bullet in (speaker.bullets or []):
                lines.append(f"- {bullet}")
            lines.append("")

    # ── Summary ─────────────────────────────────────────────
    if payload.summary:
        lines.append("## Summary")
        lines.append(payload.summary)
        lines.append("")

    # ── Key Decision ────────────────────────────────────────
    if payload.decision_headline:
        lines.append(f"## Key Decision: {payload.decision_headline}")
        if payload.decision_chosen:
            lines.append(payload.decision_chosen)
        if payload.decision_rejected:
            lines.append(f"*Rejected: {payload.decision_rejected}*")
        lines.append("")
    elif payload.key_decisions:
        lines.append("## Key Decisions")
        for item in payload.key_decisions:
            lines.append(f"- {item}")
        lines.append("")

    # ── Strategic Discussions ───────────────────────────────
    if payload.strategic_discussions:
        lines.append("## Strategic Discussions")
        lines.append("")
        for disc in payload.strategic_discussions:
            lines.append(f"### {disc.topic}")
            lines.append(disc.content)
            lines.append("")

    # ── Action Items ────────────────────────────────────────
    lines.append("## Action Items")
    if payload.structured_action_items:
        for ai in payload.structured_action_items:
            lines.append(f"- **{ai.who}**: {ai.action}")
    elif payload.action_items:
        for item in payload.action_items:
            lines.append(f"- {item}")
    else:
        lines.append("- None")
    lines.append("")

    # ── Risks ───────────────────────────────────────────────
    lines.append("## Risks")
    if payload.structured_risks:
        for r in payload.structured_risks:
            if r.impact:
                lines.append(f"- **{r.risk}**: {r.impact}")
            else:
                lines.append(f"- {r.risk}")
    elif payload.risks:
        for item in payload.risks:
            lines.append(f"- {item}")
    else:
        lines.append("- None")
    lines.append("")

    lines.append(f"Confidence: {payload.confidence or 'unknown'}")
    if payload.confidence_notes:
        lines.append(payload.confidence_notes)

    return "\n".join(lines).strip()


def _code_tag_text(text: str) -> str:
    """Wrap code-like references (file paths, class names, config keys, API names) in <code> tags."""
    # Match patterns: file paths, dotted identifiers, config keys, backtick-wrapped text
    # Pattern 1: backtick-wrapped `code`
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    # Pattern 2: file paths like src/foo/bar.py, /etc/config
    text = re.sub(r"(?<!<code>)(?:^|(?<=\s))([/\w][\w/._-]*\.(?:py|yaml|yml|json|md|ts|tsx|js|sh|toml|cfg|ini|html|css))\b", r"<code>\1</code>", text)
    # Pattern 3: dotted identifiers like ClassName.method, config.key.value (2+ dots or PascalCase)
    text = re.sub(r"(?<![<\w])([A-Z]\w+(?:\.\w+)+)", r"<code>\1</code>", text)
    return text


def _esc(text: str) -> str:
    """HTML-escape text, then apply code tagging."""
    return _code_tag_text(html.escape(text, quote=False))


def _render_summary_html(
    payload: TeamsMeetingSummaryPayload,
    template: dict[str, Any] | None = None,
) -> str:
    """Render a meeting summary as HTML following the template structure.

    Falls back to a minimal hardcoded layout when template is None.
    """
    parts: list[str] = []

    # ── Header ──────────────────────────────────────────────
    title = html.escape(payload.title or f"Meeting {payload.meeting_ref.meeting_id}", quote=False)
    date_str = ""
    if payload.start_time:
        date_str = payload.start_time.strftime("%b %d, %Y")
    time_range = ""
    if payload.start_time and payload.end_time:
        time_range = f"{payload.start_time.strftime('%H:%M')}–{payload.end_time.strftime('%H:%M')} UTC"
    duration = ""
    if payload.start_time and payload.end_time:
        mins = int((payload.end_time - payload.start_time).total_seconds() / 60)
        duration = f"~{mins} min"

    organizer = ""
    attendees_list: list[str] = list(payload.participants) if payload.participants else []
    # Try to extract organizer from meeting_ref metadata
    if payload.meeting_ref.metadata:
        organizer = payload.meeting_ref.metadata.get("organizer", "")

    header_parts = [f"<h2>📋 Meeting Summary: {title}</h2>"]
    meta_parts = []
    if date_str or time_range:
        date_meta = date_str
        if time_range:
            date_meta += f" · {time_range}"
        if duration:
            date_meta += f" ({duration})"
        meta_parts.append(f"<b>Date:</b> {date_meta}")
    if organizer:
        meta_parts.append(f"<b>Organizer:</b> {html.escape(organizer, quote=False)}")
    if attendees_list:
        meta_parts.append(f"<b>Attendees:</b> {html.escape(', '.join(attendees_list), quote=False)}")
    if meta_parts:
        header_parts.append("<p>" + "<br>\n".join(meta_parts) + "</p>")

    parts.append("\n".join(header_parts))

    # ── Status Updates (per-speaker) ────────────────────────
    if payload.speakers:
        section_parts = ["<h3>📊 Status Updates &amp; Code Exploration</h3>"]
        for speaker in payload.speakers:
            topic = html.escape(speaker.topic_label, quote=False) if speaker.topic_label else ""
            name = html.escape(speaker.name, quote=False)
            label = f"{name} — {topic}" if topic else name
            section_parts.append(f"<p><b>{label}</b></p>")
            if speaker.bullets:
                section_parts.append("<ul>")
                for bullet in speaker.bullets:
                    section_parts.append(f"  <li>{_esc(bullet)}</li>")
                section_parts.append("</ul>")
        parts.append("\n".join(section_parts))

    # ── Key Decision ────────────────────────────────────────
    if payload.decision_headline:
        decision_parts = [f"<h3>🎯 Key Decision: {html.escape(payload.decision_headline, quote=False)}</h3>"]
        if payload.decision_chosen:
            decision_parts.append(f"<p>{_esc(payload.decision_chosen)}</p>")
        if payload.decision_rejected:
            decision_parts.append(f"<p><i>Rejected: {_esc(payload.decision_rejected)}</i></p>")
        parts.append("\n".join(decision_parts))

    # ── Strategic Discussions ───────────────────────────────
    if payload.strategic_discussions:
        for disc in payload.strategic_discussions:
            disc_parts = [f"<h3>📌 {html.escape(disc.topic, quote=False)}</h3>"]
            disc_parts.append(f"<p>{_esc(disc.content)}</p>")
            parts.append("\n".join(disc_parts))

    # ── Action Items Table ──────────────────────────────────
    action_items: list[tuple[str, str]] = []
    if payload.structured_action_items:
        for ai in payload.structured_action_items:
            action_items.append((ai.who, ai.action))
    elif payload.action_items:
        # Legacy format: list of strings — try to extract "Name: action" or just show as-is
        for item in payload.action_items:
            if ":" in item:
                who, _, action = item.partition(":")
                action_items.append((who.strip(), action.strip()))
            else:
                action_items.append(("Team", item))

    if action_items:
        # Sort alphabetically by Who; "Team"/shared items last
        def _sort_key(row: tuple[str, str]) -> tuple[int, str]:
            who_lower = row[0].lower()
            is_team = who_lower in ("team", "all", "shared")
            return (1 if is_team else 0, who_lower)

        action_items.sort(key=_sort_key)

        table_rows = ["<table>", "<tr><th>Who</th><th>Action</th></tr>"]
        for who, action in action_items:
            table_rows.append(
                f"<tr><td><b>{html.escape(who, quote=False)}</b></td>"
                f"<td>{_esc(action)}</td></tr>"
            )
        table_rows.append("</table>")
        parts.append("<h3>✅ Action Items</h3>\n" + "\n".join(table_rows))

    # ── Risks Table ─────────────────────────────────────────
    risks: list[tuple[str, str]] = []
    if payload.structured_risks:
        for r in payload.structured_risks:
            risks.append((r.risk, r.impact))
    elif payload.risks:
        for item in payload.risks:
            risks.append((item, ""))

    if risks:
        risk_rows = ["<table>", "<tr><th>Risk</th><th>Impact</th></tr>"]
        for risk, impact in risks:
            risk_rows.append(
                f"<tr><td>{_esc(risk)}</td>"
                f"<td>{_esc(impact) if impact else '—'}</td></tr>"
            )
        risk_rows.append("</table>")
        parts.append("<h3>⚠️ Risks</h3>\n" + "\n".join(risk_rows))

    # ── Join with <hr> separators ───────────────────────────
    return "\n<hr>\n".join(parts)
