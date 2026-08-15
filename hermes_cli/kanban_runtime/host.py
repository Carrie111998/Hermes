"""Trusted host handlers for one strict-worker broker session."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from hermes_cli.kanban_store.claims import heartbeat, verify_fence
from hermes_cli.kanban_store.events import append_event
from hermes_cli.kanban_store.finalization import finalize_worker_run
from hermes_cli.kanban_store.publication import PolicyResolver
from hermes_cli.kanban_store.types import (
    ArtifactDeclaration,
    DraftIntent,
    EventRecord,
    FinalizationRequest,
    PublicationKind,
    RunFence,
    RuntimeIdentity,
    WorkerContext,
)

from .broker import BrokerSession, SessionState
from .capabilities import CapabilityManifest

InferenceHandler = Callable[[Mapping[str, Any], RuntimeIdentity], Mapping[str, Any]]


class StrictWorkerHost:
    """One generation-bound host authority; workers only see framed methods."""

    def __init__(
        self,
        *,
        conn,
        context: WorkerContext,
        manifest: CapabilityManifest,
        workspace: str | Path,
        artifact_blob_root: str | Path,
        policy_resolver: PolicyResolver,
        inference_handler: InferenceHandler,
    ) -> None:
        self.conn = conn
        self.context = context
        self.manifest = manifest
        self.workspace = Path(workspace).resolve()
        self.artifact_blob_root = Path(artifact_blob_root).resolve()
        self.policy_resolver = policy_resolver
        self.inference_handler = inference_handler
        self._drafts: dict[str, DraftIntent] = {}
        self._artifacts: dict[str, ArtifactDeclaration] = {}
        self.state = SessionState(
            context.fence, runtime_identity=context.runtime_identity
        )

    def session(self) -> BrokerSession:
        return BrokerSession(
            state=self.state,
            manifest=self.manifest,
            fence_validator=lambda fence: verify_fence(self.conn, fence),
            handlers={
                "inference.request": self._inference,
                "event.append": self._event,
                "intent.draft": self._intent,
                "artifact.declare": self._artifact,
                "heartbeat": self._heartbeat,
                "finalize": self._finalize,
            },
        )

    def _inference(self, params: Mapping[str, Any], _fence: RunFence):
        return self.inference_handler(params, self.context.runtime_identity)

    def _event(self, params: Mapping[str, Any], fence: RunFence):
        event = EventRecord(
            event_uuid=str(params["event_uuid"]),
            task_id=fence.task_id,
            run_id=fence.run_id,
            claim_generation=fence.claim_generation,
            event_type=str(params["event_type"]),
            source="strict-worker",
            severity=str(params["severity"]),
            retention_class=str(params["retention_class"]),
            payload=dict(params["payload"]),
            correlation_id=params.get("correlation_id"),
            operation_id=params.get("operation_id"),
            stream=params.get("stream"),
            stream_seq=params.get("stream_seq"),
            producer_time=params.get("producer_time"),
        )
        return {"event_seq": append_event(self.conn, event)}

    def _intent(self, params: Mapping[str, Any], _fence: RunFence):
        draft = DraftIntent(
            kind=PublicationKind(str(params["kind"])),
            target=dict(params["target"]),
            payload=dict(params["payload"]),
            client_nonce=str(params["client_nonce"]),
        )
        existing = self._drafts.get(draft.client_nonce)
        if existing is not None and existing != draft:
            raise ValueError("client_nonce was reused for different intent bytes")
        self._drafts[draft.client_nonce] = draft
        return {"accepted": True, "client_nonce": draft.client_nonce}

    def _artifact(self, params: Mapping[str, Any], _fence: RunFence):
        declaration = ArtifactDeclaration(
            relative_path=str(params["relative_path"]),
            display_name=str(params["display_name"]),
            media_type=str(params["media_type"]),
        )
        existing = self._artifacts.get(declaration.relative_path)
        if existing is not None and existing != declaration:
            raise ValueError("artifact path was redeclared with different metadata")
        self._artifacts[declaration.relative_path] = declaration
        return {"accepted": True, "relative_path": declaration.relative_path}

    def _heartbeat(self, params: Mapping[str, Any], fence: RunFence):
        return {
            "claim_expires": heartbeat(
                self.conn, fence, ttl_seconds=int(params["ttl_seconds"])
            )
        }

    def _finalize(self, params: Mapping[str, Any], fence: RunFence):
        trusted = RuntimeIdentity(**dict(params["_trusted_runtime_identity"]))
        if trusted != self.context.runtime_identity:
            raise PermissionError("broker runtime identity drifted")
        metadata = dict(params.get("metadata") or {})
        if "runtime_identity" in metadata:
            raise PermissionError("runtime_identity is host-reserved")
        request = FinalizationRequest(
            fence=fence,
            outcome=str(params["outcome"]),
            summary=str(params["summary"]),
            metadata=metadata,
            artifacts=tuple(self._artifacts[key] for key in sorted(self._artifacts)),
            draft_intents=tuple(self._drafts[key] for key in sorted(self._drafts)),
        )
        return finalize_worker_run(
            self.conn,
            request=request,
            workspace=self.workspace,
            artifact_blob_root=self.artifact_blob_root,
            policy_resolver=self.policy_resolver,
            trusted_runtime_identity=trusted,
        )
