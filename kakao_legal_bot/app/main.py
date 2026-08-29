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
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

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
    messages = []
    for row in rows:
        # room_name 도 동봉합니다 — 폰 브리지(무루팅, 알림 기반)는 방을
        # 이름으로만 특정할 수 있어서, id→이름 매핑이 날아가도 이걸로 배달합니다.
        room = await asyncio.to_thread(services.db.get_room, str(row["room_id"]))
        messages.append(
            {
                "id": row["id"],
                "room": row["room_id"],
                "room_name": str(room["room_name"]) if room is not None else "",
                "text": row["text"],
            }
        )
    return JSONResponse({"messages": messages})


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


# ── 증거 사진·파일 접수 (상담자용 공개 페이지) ──────────────────────────
_UPLOAD_PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>자료 올리기</title>
<style>
 body {{ font-family: -apple-system, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
        max-width: 480px; margin: 0 auto; padding: 32px 20px; line-height: 1.6; }}
 h1 {{ font-size: 20px; }}
 .note {{ background: rgba(128,128,128,.12); padding: 10px 14px; border-radius: 10px;
         font-size: 14px; }}
 input[type=file] {{ width: 100%; padding: 14px 0; }}
 button {{ width: 100%; padding: 14px; font-size: 16px; border-radius: 10px;
          border: none; background: #391b1b; color: #fff; cursor: pointer; }}
 .ok {{ border-left: 4px solid #2e7d32; }}
</style></head><body>
<h1>📎 상담 자료 올리기</h1>
{banner}
<p class="note">사진·문서·녹음 등 사건 관련 자료를 올려주세요.
올려주시는 즉시 변호사님께 전달되어 상담에 반영됩니다.
(파일 하나 최대 {max_mb}MB{count})</p>
<form method="post" enctype="multipart/form-data">
  <input type="file" name="files" multiple required>
  <button type="submit">올리기</button>
</form>
</body></html>"""


def _upload_room(services: Services, token: str) -> str:
    from .uploads import room_for_upload_token

    room_id = room_for_upload_token(services.db, token)
    if not room_id:
        raise HTTPException(404, "주소가 올바르지 않습니다.")
    return room_id


@router.get("/u/{token}", response_class=HTMLResponse)
async def upload_page(token: str, request: Request) -> HTMLResponse:
    services: Services = request.app.state.services
    room_id = await asyncio.to_thread(_upload_room, services, token)
    received = await asyncio.to_thread(services.db.count_uploads, room_id)
    count = f" · 지금까지 {received}건 접수" if received else ""
    return HTMLResponse(
        _UPLOAD_PAGE.format(banner="", max_mb=services.settings.upload_max_mb, count=count)
    )


@router.post("/u/{token}", response_class=HTMLResponse)
async def upload_files(token: str, request: Request) -> HTMLResponse:
    """상담자 폰에서 바로 올라오는 증거 자료.

    저장 → 변호사 카톡 📎 알림 → 대화 기록에 시스템 메시지(AI 가 자료
    도착을 인지) → 상담방에 접수 확인, 이 네 가지가 한 번에 일어납니다.
    """
    services: Services = request.app.state.services
    settings = services.settings
    room_id = await asyncio.to_thread(_upload_room, services, token)

    from .uploads import human_size, save_upload

    form = await request.form()
    limit = settings.upload_max_mb * 1024 * 1024
    saved: list[str] = []
    rejected: list[str] = []
    for item in form.getlist("files"):
        if isinstance(item, str) or not getattr(item, "filename", ""):
            continue
        content = await item.read()
        if not content:
            continue
        if len(content) > limit:
            rejected.append(item.filename)
            continue
        _, _path = await asyncio.to_thread(
            save_upload, settings, services.db, room_id,
            item.filename, content, item.content_type or "",
        )
        saved.append(f"{item.filename} ({human_size(len(content))})")

    if saved:
        listing = "\n".join(f"- {name}" for name in saved)
        # AI 가 다음 턴에 자료 도착을 알도록 대화 기록에 남깁니다.
        await asyncio.to_thread(
            services.db.add_message,
            room_id,
            "system",
            f"[자료 접수] 상담자가 파일 {len(saved)}건을 올렸습니다:\n{listing}",
            "",
            "",
            settings.history_turns,
            settings.archive_enabled,
        )
        room = await asyncio.to_thread(services.db.get_room, room_id)
        label = str(room["label"]) if room is not None and "label" in room.keys() else ""
        link = settings.admin_url(f"/rooms/{room_id}")
        _spawn(
            services.sender.notify_lawyer(
                f"📎 자료 {len(saved)}건 접수\n방: {label or room_id}\n{listing}"
                + (f"\n바로 보기: {link}" if link else "")
            )
        )
        with contextlib.suppress(Exception):
            await services.sender.send(
                room_id,
                f"자료 {len(saved)}건을 접수했습니다. 변호사님께 바로 전달드렸습니다. 📎",
                record_role="",
            )

    banner = ""
    if saved:
        banner = f'<p class="note ok">✅ {len(saved)}건 접수되었습니다. 감사합니다.</p>'
    if rejected:
        banner += (
            f'<p class="note">⚠ 용량 초과로 받지 못한 파일 {len(rejected)}건: '
            f"{', '.join(rejected[:5])} — {settings.upload_max_mb}MB 이하로 나눠 올려주세요.</p>"
        )
    if not saved and not rejected:
        banner = '<p class="note">파일이 선택되지 않았습니다.</p>'
    received = await asyncio.to_thread(services.db.count_uploads, room_id)
    return HTMLResponse(
        _UPLOAD_PAGE.format(
            banner=banner,
            max_mb=settings.upload_max_mb,
            count=f" · 지금까지 {received}건 접수" if received else "",
        )
    )


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


_SYNC_KEY = "law_sync_last_run"


async def run_law_sync_if_due(services: Services, *, force: bool = False) -> str:
    """개정 법령·최근 판례를 확인하고 변호사에게 알린다.

    서버는 **찾아서 알리는 데까지만** 합니다. 글을 고치는 것은 PC의 코덱스
    몫이에요 — 서버가 자료를 고치기 시작하면 무엇이 근거였는지 알 수 없게
    되고, 실패했을 때 되돌릴 수도 없습니다.
    """
    settings = services.settings
    if not settings.law_sync_enabled or services.law is None:
        return ""
    now = time.time()
    if not force:
        last = await asyncio.to_thread(services.db.kv_get, _SYNC_KEY, "0")
        try:
            elapsed = now - float(last or 0)
        except ValueError:
            elapsed = now
        if elapsed < max(settings.law_sync_interval_h, 1) * 3600:
            return ""
    await asyncio.to_thread(services.db.kv_set, _SYNC_KEY, str(now))

    from .wiki.sync import LawSync, watched_laws

    if services.graph is None:
        log.info("law sync: 위키 그래프가 없어 지켜볼 법령을 모릅니다")
        return ""
    laws = await asyncio.to_thread(watched_laws, services.graph, settings.law_sync_top_laws)
    if not laws:
        return ""

    sync = LawSync(services.law, settings.wiki_vault, services.graph)
    result = await sync.sync_laws(laws)
    cases = await sync.sync_precedents(
        laws, since=(date.today() - timedelta(days=settings.law_sync_precedent_days)).isoformat()
    )
    result.cases.extend(cases.cases)
    result.errors.extend(cases.errors)
    await asyncio.to_thread(sync.write_worklist, result)

    if not result.changed:
        log.info("law sync: 바뀐 것이 없습니다 (법령 %s건 확인)", len(laws))
        return ""

    lines = [f"📜 법령·판례 변동 알림 — {result.summary()}"]
    for update in result.laws[:10]:
        mark = "신규" if update.is_new else f"{update.previous} → {update.effective_on}"
        lines.append(f"· {update.law} 개정 ({mark})")
    for update in result.cases[:10]:
        lines.append(f"· {update.court} {update.case_no} ({update.decided_on})")
    if result.affected:
        lines.append(f"\n개정 이후 확인이 필요한 기존 자료 {len(result.affected)}건:")
        for item in result.affected[:5]:
            lines.append(f"  - {item['title']} ({item['statute']})")
        if len(result.affected) > 5:
            lines.append(f"  - … 그 밖에 {len(result.affected) - 5}건")
    lines.append("\nPC에서 코덱스로 정리하시려면: python -m kakao_legal_bot.app.wiki.sync daily")
    message = "\n".join(lines)
    await services.sender.notify_lawyer(message)
    return message


async def _janitor(app: FastAPI) -> None:
    """Hourly housekeeping: retention, stuck outbox rows."""
    services: Services = app.state.services
    while True:
        try:
            await asyncio.sleep(3600)
            purged = await asyncio.to_thread(
                services.db.purge_old_messages, services.settings.history_retention_days
            )
            # 보관소는 기본 영구(0) — 기간을 명시했을 때만 정리합니다.
            await asyncio.to_thread(
                services.db.purge_old_archive, services.settings.archive_retention_days
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
            await run_law_sync_if_due(services)
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
