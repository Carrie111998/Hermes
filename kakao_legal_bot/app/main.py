"""FastAPI entrypoint.

    uvicorn kakao_legal_bot.app.main:app --host 0.0.0.0 --port ${PORT:-8000}

The webhook does the minimum amount of work that must happen synchronously
(parse, dedupe, hand off) and returns. Everything else is a task.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .admin import router as admin_router
from .config import Settings, get_settings
from .iris import IrisEvent
from .pipeline import Pipeline
from .services import Services, build_services
from .workflows import notify_draft_ready

log = logging.getLogger(__name__)

router = APIRouter()

# Background tasks need a strong reference or the loop may collect them
# mid-flight. This set is that reference.
_TASKS: set[asyncio.Task[Any]] = set()


def _spawn(coro: Any) -> None:
    task = asyncio.create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def _check_webhook_secret(settings: Settings, request: Request, header_value: str) -> None:
    expected = settings.iris_webhook_secret
    if not expected:
        return
    supplied = header_value or request.query_params.get("secret", "")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, "invalid webhook secret")


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    services: Services = request.app.state.services
    settings = services.settings
    stats = services.rag.stats() if services.rag is not None else {}
    depth = await asyncio.to_thread(services.db.outbox_depth)
    return JSONResponse(
        {
            "ok": True,
            "bot": settings.bot_name,
            "provider": settings.llm_provider,
            "model": settings.llm_model,
            "send_mode": settings.iris_send_mode,
            "law_api": services.law is not None,
            "rag": stats,
            "outbox_queued": depth,
            "draft_generator": settings.draft_generator,
            "draft_jobs_queued": await asyncio.to_thread(services.db.draft_queue_depth),
            "missing_config": settings.missing_required(),
        }
    )


@router.post("/iris/webhook")
async def iris_webhook(
    request: Request,
    x_iris_secret: str = Header(default=""),
) -> JSONResponse:
    """Iris posts every KakaoTalk message here.

    Returns in single-digit milliseconds: the answer is produced on a task
    so a slow model can never turn into a webhook timeout on Iris's side.
    """
    services: Services = request.app.state.services
    _check_webhook_secret(services.settings, request, x_iris_secret)

    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        # A malformed body is Iris's problem, not a reason to 500 and make
        # it retry forever.
        return JSONResponse({"ok": False, "reason": "invalid json"}, status_code=200)

    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "reason": "unexpected payload"}, status_code=200)

    event = IrisEvent.parse(payload)
    if not event.room_id:
        return JSONResponse({"ok": False, "reason": "no room"}, status_code=200)

    fresh = await asyncio.to_thread(services.db.mark_seen, event.event_key)
    if not fresh:
        return JSONResponse({"ok": True, "reason": "duplicate"})

    pipeline: Pipeline = request.app.state.pipeline
    _spawn(pipeline.handle(event))
    return JSONResponse({"ok": True})


@router.get("/outbox")
async def outbox_pull(request: Request, x_outbox_token: str = Header(default="")) -> JSONResponse:
    """Relay-mode delivery: the box next to the emulator pulls from here."""
    services: Services = request.app.state.services
    token = services.settings.outbox_token
    supplied = x_outbox_token or request.query_params.get("token", "")
    if not token or not hmac.compare_digest(supplied, token):
        raise HTTPException(401, "invalid outbox token")

    limit = min(int(request.query_params.get("limit", 10)), 50)
    rows = await asyncio.to_thread(services.db.claim_outbox, limit)
    return JSONResponse(
        {"messages": [{"id": row["id"], "room": row["room_id"], "text": row["text"]} for row in rows]}
    )


@router.post("/outbox/ack")
async def outbox_ack(request: Request, x_outbox_token: str = Header(default="")) -> JSONResponse:
    services: Services = request.app.state.services
    token = services.settings.outbox_token
    supplied = x_outbox_token or request.query_params.get("token", "")
    if not token or not hmac.compare_digest(supplied, token):
        raise HTTPException(401, "invalid outbox token")

    body = await request.json()
    ids = [int(value) for value in (body.get("ids") or [])]
    ok = bool(body.get("ok", True))
    await asyncio.to_thread(services.db.ack_outbox, ids, ok, str(body.get("error") or ""))
    return JSONResponse({"ok": True, "acked": len(ids)})


def _require_worker_token(services: Services, request: Request, header_value: str) -> None:
    token = services.settings.draft_worker_token
    supplied = header_value or request.query_params.get("token", "")
    if not token or not hmac.compare_digest(supplied, token):
        raise HTTPException(401, "invalid draft worker token")


@router.get("/drafts/queue")
async def draft_queue(
    request: Request, x_worker_token: str = Header(default="")
) -> JSONResponse:
    """The Codex worker on the lawyer's PC pulls document jobs from here.

    Jobs are *claimed*, not just listed, so a restarted worker cannot write
    the same document twice.
    """
    services: Services = request.app.state.services
    _require_worker_token(services, request, x_worker_token)

    limit = min(int(request.query_params.get("limit", 1)), 5)
    jobs = await asyncio.to_thread(services.db.claim_draft_jobs, limit)
    return JSONResponse(
        {
            "jobs": [
                {
                    "id": job.id,
                    "kind": job.kind,
                    "title": job.title,
                    "instructions": job.instructions,
                    "transcript": job.transcript,
                    "room_id": job.room_id,
                    "attempts": job.attempts,
                }
                for job in jobs
            ]
        }
    )


@router.post("/drafts/{draft_id}/result")
async def draft_result(
    draft_id: int, request: Request, x_worker_token: str = Header(default="")
) -> JSONResponse:
    """The worker delivers a finished document; it enters the review queue."""
    services: Services = request.app.state.services
    _require_worker_token(services, request, x_worker_token)

    body = await request.json()
    text = str(body.get("body") or "").strip()
    if not text:
        raise HTTPException(400, "body is empty")

    ok = await asyncio.to_thread(
        services.db.complete_draft_generation, draft_id, text, str(body.get("title") or "")
    )
    if not ok:
        # Already delivered, or the lawyer edited it in the meantime.
        return JSONResponse({"ok": False, "reason": "not awaiting generation"}, status_code=409)

    _spawn(notify_draft_ready(services, draft_id))
    return JSONResponse({"ok": True})


@router.post("/drafts/{draft_id}/fail")
async def draft_failed(
    draft_id: int, request: Request, x_worker_token: str = Header(default="")
) -> JSONResponse:
    """The worker could not produce a document — retry, then tell the lawyer."""
    services: Services = request.app.state.services
    _require_worker_token(services, request, x_worker_token)

    body = await request.json()
    error = str(body.get("error") or "unknown error")
    status = await asyncio.to_thread(
        services.db.fail_draft_generation, draft_id, error, services.settings.draft_max_attempts
    )
    if status == "generation_failed":
        _spawn(
            services.sender.notify_lawyer(
                f"⚠️ 초안 #{draft_id} 자동 작성에 실패했습니다 — 직접 작성이 필요합니다.\n"
                f"사유: {error[:300]}"
            )
        )
    return JSONResponse({"ok": True, "status": status})


@router.get("/", response_class=PlainTextResponse)
async def index() -> PlainTextResponse:
    return PlainTextResponse("moa legal bot is running. POST /iris/webhook")


async def _janitor(app: FastAPI) -> None:
    """Hourly housekeeping: retention, stuck outbox rows."""
    services: Services = app.state.services
    while True:
        try:
            await asyncio.sleep(3600)
            purged = await asyncio.to_thread(
                services.db.purge_old_messages, services.settings.history_retention_days
            )
            requeued = await asyncio.to_thread(services.db.requeue_stale_outbox, 300.0)
            # A worker that died mid-document must not strand the request.
            drafts = await asyncio.to_thread(
                services.db.requeue_stale_draft_jobs, services.settings.draft_job_timeout_s
            )
            if purged or requeued or drafts:
                log.info(
                    "janitor: purged=%s requeued=%s drafts_requeued=%s", purged, requeued, drafts
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("janitor iteration failed")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    services = getattr(app.state, "services", None) or build_services(settings)
    app.state.services = services
    app.state.pipeline = Pipeline(services)
    app.state.started_at = time.time()

    missing = settings.missing_required()
    if missing:
        log.warning("설정이 비어 있습니다: %s — 해당 기능은 동작하지 않습니다", ", ".join(missing))

    janitor = asyncio.create_task(_janitor(app))
    try:
        yield
    finally:
        janitor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await janitor
        for task in list(_TASKS):
            task.cancel()
        await services.aclose()


def create_app(services: Services | None = None) -> FastAPI:
    app = FastAPI(title="모아 — 카카오톡 법률상담 어시스턴트", lifespan=lifespan)
    if services is not None:
        app.state.services = services
    app.include_router(router)
    app.include_router(admin_router)
    return app


app = create_app()
