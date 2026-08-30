"""Hermes-native participant runner for Fusion v2."""

from __future__ import annotations

import hashlib
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Callable

from .context import FusionContext
from .models import FusionParticipantResult, FusionParticipantSpec, FusionRequest
from .prompts import build_participant_system_prompt, build_participant_user_prompt

ProgressCallback = Callable[[str, dict], None]


def _hash_output(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _parent_attr(parent_agent, name: str, default=None):
    return getattr(parent_agent, name, default) if parent_agent is not None else default


def _reasoning_config(spec: FusionParticipantSpec, request: FusionRequest, parent_agent):
    effort = spec.reasoning_effort or request.reasoning_effort
    if effort:
        try:
            from hermes_constants import parse_reasoning_effort

            parsed = parse_reasoning_effort(effort)
            if parsed is not None:
                return parsed
        except Exception:
            pass
    return _parent_attr(parent_agent, "reasoning_config")


def run_participant_turn(
    spec: FusionParticipantSpec,
    request: FusionRequest,
    context: FusionContext,
    *,
    phase: str,
    phase_prompt: str,
    parent_agent=None,
    progress_callback: ProgressCallback | None = None,
    toolset: str = "fusion_readonly",
    write_root: str | None = None,
) -> FusionParticipantResult:
    """Run one Fusion participant phase as a fresh scoped child ``AIAgent``."""
    started = time.monotonic()
    task_id = f"fusion-{phase}-{spec.slug}-{uuid.uuid4().hex[:8]}"
    if progress_callback:
        progress_callback("participant_phase_start", {"slug": spec.slug, "phase": phase, "model": spec.runtime_label})

    try:
        from run_agent import AIAgent
        from tools.file_tools import (
            register_fusion_readonly_root,
            register_fusion_write_root,
            unregister_fusion_readonly_root,
            unregister_fusion_write_root,
        )
    except Exception as exc:
        return FusionParticipantResult(
            spec=spec,
            status="error",
            phase=phase,
            error=f"Could not initialize Fusion participant runtime: {exc}",
            duration_seconds=round(time.monotonic() - started, 2),
        )

    repo_root = context.repo_root
    scope_root = write_root if toolset == "fusion_spike" and write_root else repo_root
    if toolset == "fusion_spike" and write_root:
        register_fusion_write_root(task_id, write_root)
    elif repo_root:
        register_fusion_readonly_root(task_id, repo_root)

    child = None
    try:
        provider = spec.provider or _parent_attr(parent_agent, "provider")
        model = spec.model or _parent_attr(parent_agent, "model", "")
        api_mode = spec.api_mode or _parent_attr(parent_agent, "api_mode")
        child = AIAgent(
            base_url=_parent_attr(parent_agent, "base_url"),
            api_key=_parent_attr(parent_agent, "api_key"),
            provider=provider,
            api_mode=api_mode,
            model=model,
            max_iterations=20,
            enabled_toolsets=[toolset],
            quiet_mode=True,
            ephemeral_system_prompt=build_participant_system_prompt(spec, context, phase=phase),
            platform=_parent_attr(parent_agent, "platform", "cli"),
            skip_context_files=True,
            skip_memory=True,
            session_db=_parent_attr(parent_agent, "_session_db"),
            parent_session_id=_parent_attr(parent_agent, "session_id"),
            fallback_model=_parent_attr(parent_agent, "_fallback_chain"),
            credential_pool=_parent_attr(parent_agent, "_credential_pool"),
            reasoning_config=_reasoning_config(spec, request, parent_agent),
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(child.run_conversation, phase_prompt, task_id=task_id)
            try:
                raw = future.result(timeout=request.timeout_seconds)
            except FuturesTimeoutError:
                try:
                    child.interrupt("Fusion participant timeout")
                except Exception:
                    pass
                executor.shutdown(wait=False, cancel_futures=True)
                return FusionParticipantResult(
                    spec=spec,
                    status="timeout",
                    phase=phase,
                    error=f"Participant timed out after {request.timeout_seconds}s",
                    duration_seconds=round(time.monotonic() - started, 2),
                    model=getattr(child, "model", None),
                    provider=provider,
                )

        output = ""
        api_calls = 0
        if isinstance(raw, dict):
            output = str(raw.get("final_response") or "")
            api_calls = int(raw.get("api_calls") or 0)
            if not output and raw.get("error"):
                raise RuntimeError(str(raw.get("error")))
        else:
            output = str(raw or "")
        status = "completed" if output.strip() else "failed"
        result = FusionParticipantResult(
            spec=spec,
            status=status,
            phase=phase,
            output=output,
            error=None if status == "completed" else "Participant produced no output.",
            duration_seconds=round(time.monotonic() - started, 2),
            api_calls=api_calls,
            model=getattr(child, "model", None) or model,
            provider=provider,
            metadata={
                "task_id": task_id,
                "toolsets": [toolset],
                "scope_root": scope_root,
                "write_root": write_root if toolset == "fusion_spike" else None,
                "phase": phase,
                "requested_provider": spec.requested_provider,
                "requested_model": spec.requested_model,
                "provider": provider,
                "model": getattr(child, "model", None) or model,
                "api_mode": api_mode,
                "reasoning_effort": spec.reasoning_effort or request.reasoning_effort,
            },
            output_hash=_hash_output(output) if output else None,
        )
        if progress_callback:
            progress_callback("participant_phase_complete", {"slug": spec.slug, "phase": phase, "status": result.status})
        return result
    except Exception as exc:
        return FusionParticipantResult(
            spec=spec,
            status="error",
            phase=phase,
            error=str(exc),
            duration_seconds=round(time.monotonic() - started, 2),
            model=getattr(child, "model", None) if child is not None else spec.model,
            provider=spec.provider,
        )
    finally:
        if toolset == "fusion_spike" and write_root:
            unregister_fusion_write_root(task_id)
        elif repo_root:
            unregister_fusion_readonly_root(task_id)
        if child is not None:
            try:
                child.close()
            except Exception:
                pass


def run_participant(
    spec: FusionParticipantSpec,
    request: FusionRequest,
    context: FusionContext,
    *,
    parent_agent=None,
    progress_callback: ProgressCallback | None = None,
    phase: str = "draft",
    phase_prompt: str | None = None,
    toolset: str = "fusion_readonly",
    write_root: str | None = None,
) -> FusionParticipantResult:
    """Backward-compatible entry point for a participant phase."""
    return run_participant_turn(
        spec,
        request,
        context,
        phase=phase,
        phase_prompt=phase_prompt or build_participant_user_prompt(spec, request, context),
        parent_agent=parent_agent,
        progress_callback=progress_callback,
        toolset=toolset,
        write_root=write_root,
    )
