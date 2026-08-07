"""Compute-host turn-isolation helpers (moved verbatim from tui_gateway/server.py).

The functions in this module are byte-identical to their pre-split
server.py bodies.  server.py imports this module at the end of its own
import and rebinds every function onto its namespace with
``types.FunctionType`` (same seam as the methods_* handler split — see
method_ctx.py), so all module-global references (``write_json``,
``_ok``/``_err``, ``_session_cwd``, ``_session_source``,
``_compute_host_supervisor``, ``_load_dashboard_process_isolation_config``,
..., and the s3 helpers ``_session_info``/``_clear_inflight_turn``/
``_drain_queued_prompt``) keep resolving against server.py's namespace
exactly as before the move.
"""

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


def _inside_compute_host_child() -> bool:
    return os.environ.get("HERMES_COMPUTE_HOST_CHILD") == "1"


def _turn_isolation_enabled(cfg: dict | None = None) -> bool:
    if _inside_compute_host_child():
        return False
    isolation_cfg = cfg or _load_dashboard_process_isolation_config()
    return bool(isolation_cfg.get("turn_isolation"))


def _session_uses_compute_host(session: dict, cfg: dict | None = None) -> bool:
    if not _turn_isolation_enabled(cfg):
        return False
    # Phase 1 routes lazy/dashboard sessions whose live AIAgent has not been
    # built inside the serving process. Already-built in-process sessions keep
    # the historical path unless a prior isolated turn marked host ownership.
    return bool(session.get("_compute_host_active")) or (
        session.get("agent") is None and session.get("agent_ready") is not None
    )


def _get_compute_host_supervisor(cfg: dict | None = None):
    global _compute_host_supervisor
    isolation_cfg = cfg or _load_dashboard_process_isolation_config()
    with _compute_host_supervisor_lock:
        if _compute_host_supervisor is None:
            from tui_gateway.host_supervisor import HostSupervisor

            _compute_host_supervisor = HostSupervisor(
                rpc_sink=write_json,
                heartbeat_secs=int(isolation_cfg.get("compute_host_heartbeat_secs") or 15),
                respawn_max=int(isolation_cfg.get("compute_host_respawn_max") or 3),
            )
        return _compute_host_supervisor


def _compute_host_turn_frame(
    rid: str,
    sid: str,
    session: dict,
    text: Any,
    image_paths: list[str] | None = None,
    queued_prompt_generation: int | None = None,
) -> dict:
    with session["history_lock"]:
        history = list(session.get("history", []))
        history_version = int(session.get("history_version", 0))
        attached_images = (
            list(image_paths)
            if image_paths is not None
            else list(session.get("attached_images", []))
        )
    return {
        "type": "turn.start",
        "sid": sid,
        "request_id": rid,
        "session_key": session.get("session_key") or sid,
        "text": text,
        "history": history,
        "history_version": history_version,
        "cols": int(session.get("cols", 80) or 80),
        "cwd": _session_cwd(session),
        "profile_home": session.get("profile_home") or "",
        "model_override": session.get("model_override"),
        "reasoning_config_override": session.get("create_reasoning_override"),
        "service_tier_override": session.get("create_service_tier_override"),
        "source": _session_source(session),
        "attached_images": attached_images,
        "queued_prompt_generation": queued_prompt_generation,
    }


def _metadata_mirror(session: dict | None) -> dict:
    mirror = (session or {}).get("_metadata_mirror")
    return mirror if isinstance(mirror, dict) else {}


def _apply_compute_host_metadata_mirror(session: dict, frame: dict | None) -> None:
    """Mirror host-owned session metadata in the serving process.

    The compute host is the only writer of live agent/history state while turn
    isolation is active. The serving process keeps read metadata from the last
    host frame so UI reads do not construct a second in-process agent.
    """
    if not isinstance(frame, dict):
        return
    with session.get("history_lock", threading.Lock()):
        if frame.get("session_key"):
            session["session_key"] = str(frame.get("session_key"))
        if frame.get("history_version") is not None:
            try:
                session["history_version"] = max(
                    int(session.get("history_version", 0)),
                    int(frame.get("history_version") or 0),
                )
            except Exception:
                pass
        if frame.get("message_count") is not None:
            try:
                session["_metadata_message_count"] = int(frame.get("message_count") or 0)
            except Exception:
                pass
    info = frame.get("session_info")
    if isinstance(info, dict):
        mirror = dict(_metadata_mirror(session))
        mirror.update(info)
        session["_metadata_mirror"] = mirror
        session["_metadata_mirror_updated_at"] = time.time()


def _on_compute_host_turn_done(rid: str, sid: str, session: dict, frame: dict) -> None:
    is_error = frame.get("type") == "turn.error"
    with session["history_lock"]:
        if frame.get("session_key"):
            session["session_key"] = str(frame.get("session_key"))
        if frame.get("history_version") is not None:
            try:
                session["history_version"] = max(
                    int(session.get("history_version", 0)),
                    int(frame.get("history_version") or 0),
                )
            except Exception:
                pass
        session["running"] = False
        session["last_active"] = time.time()
        _clear_inflight_turn(session)
    if is_error:
        message = str(frame.get("message") or "compute host turn failed")
        _emit("message.complete", sid, {"text": f"Error: {message}", "status": "error"})
    _apply_compute_host_metadata_mirror(session, frame)
    try:
        info = _session_info(session.get("agent"), session)
    except TypeError:
        info = _session_info(session.get("agent"))
    if not frame.get("session_info_emitted"):
        _emit("session.info", sid, info)
    _drain_queued_prompt(rid, sid, session)


def _submit_prompt_to_compute_host(
    rid: str,
    sid: str,
    session: dict,
    text: Any,
    image_paths: list[str] | None = None,
    queued_prompt_generation: int | None = None,
) -> dict:
    cfg = _load_dashboard_process_isolation_config()
    frame = _compute_host_turn_frame(
        rid,
        sid,
        session,
        text,
        image_paths=image_paths,
        queued_prompt_generation=queued_prompt_generation,
    )

    def _complete(done: dict) -> None:
        # submit_turn reports a synchronous pipe failure through the callback
        # before re-raising. Leave the parent session untouched so prompt.submit
        # can fail open to the historical in-process path without emitting a
        # duplicate terminal error.
        if done.get("reason") == "send_failed":
            return
        _on_compute_host_turn_done(rid, sid, session, done)

    try:
        _get_compute_host_supervisor(cfg).submit_turn(frame, on_complete=_complete)
    except Exception as exc:
        return _err(rid, 5019, f"compute-host dispatch failed: {exc}")
    with session["history_lock"]:
        session["_compute_host_active"] = True
        if image_paths is None:
            session["attached_images"] = []
    return _ok(rid, {"status": "streaming", "turn_isolation": True})


def _send_compute_host_control(
    sid: str,
    *,
    route_name: str,
    command: str = "",
    payload: dict | None = None,
    wait: bool = True,
    timeout: float = 30.0,
) -> dict:
    frame = dict(payload or {})
    frame.setdefault("type", "control")
    frame.setdefault("command", command)
    return _get_compute_host_supervisor().control(
        sid,
        route_name=route_name,
        payload=frame,
        wait=wait,
        timeout=timeout,
    )
