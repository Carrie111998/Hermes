"""Mattermost gateway adapter.

Connects to a self-hosted (or cloud) Mattermost instance via its REST API
(v4) and WebSocket for real-time events.  No external Mattermost library
required — uses aiohttp which is already a Hermes dependency.

Environment variables:
    MATTERMOST_URL              Server URL (e.g. https://mm.example.com)
    MATTERMOST_TOKEN            Bot token or personal-access token
    MATTERMOST_ALLOWED_USERS    Comma-separated user IDs
    MATTERMOST_HOME_CHANNEL     Channel ID for cron/notification delivery
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gateway.config import Platform, PlatformConfig
from gateway.platforms.helpers import MessageDeduplicator
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)

# Mattermost post size limit (server default is 16383, but 4000 is the
# practical limit for readable messages — matching OpenClaw's choice).
MAX_POST_LENGTH = 4000

# Channel type codes returned by the Mattermost API.
_CHANNEL_TYPE_MAP = {
    "D": "dm",
    "G": "group",
    "P": "group",   # private channel → treat as group
    "O": "channel",
}

_MATTERMOST_DISABLE_MENTIONS_PROPS = {"disable_mentions": True}

# Reconnect parameters (exponential backoff).
_RECONNECT_BASE_DELAY = 2.0
_RECONNECT_MAX_DELAY = 60.0
_RECONNECT_JITTER = 0.2

_HIGGSFIELD_IMAGE_MODEL_ALIASES = {
    "nano banana 2 pro": "nano_banana_pro",
    "nano banana pro": "nano_banana_pro",
    "나노 바나나 2 pro": "nano_banana_pro",
    "나노바나나 2 pro": "nano_banana_pro",
    "nano banana 2 lite": "nano_banana_2_lite",
    "gpt image 2": "gpt_image_2",
    "gpt 이미지 2": "gpt_image_2",
    "seedream 5.0 pro": "seedream_v5_pro",
    "seedream 5 pro": "seedream_v5_pro",
    "seedream 5.0 lite": "seedream_v5_lite",
    "seedream 5 lite": "seedream_v5_lite",
    "seedream 4.5": "seedream_v4_5",
    "seedream": "seedream_v4_5",
    "recraft v4.1": "recraft_v4_1",
    "recraft": "recraft_v4_1",
    "z image": "z_image",
}

_HIGGSFIELD_IMAGE_REFERENCE_JOB_TYPES = {
    "gpt_image_2",
    "nano_banana_2_lite",
    "nano_banana_pro",
    "seedream_v4_5",
    "seedream_v5_lite",
    "seedream_v5_pro",
}
_HIGGSFIELD_RESOLUTION_JOB_TYPES = {
    "gpt_image_2",
    "nano_banana_2_lite",
    "nano_banana_pro",
    "recraft_v4_1",
    "seedream_v5_pro",
}

_HIGGSFIELD_MODEL_HELP_TEXT = """Higgsfield 추천 모델

이미지/디자인
- GPT Image 2 — 일반 이미지 생성·편집, 배너/썸네일/텍스트 포함 디자인에 가장 무난합니다.
- Nano Banana Pro — 첨부 이미지 기반 편집, 스타일 변경, 인물/제품 레퍼런스 유지에 좋습니다.
- Seedream 4.5 — 얼굴/인물 유지와 복잡한 장면 합성에 강합니다.
- Seedream 5.0 Pro — Higgsfield 홈페이지 추천 최신 고품질 이미지 모델입니다.
- Recraft V4.1 — 로고, 아이콘, 벡터 스타일, 브랜드 그래픽 제작용입니다. 첨부 이미지 편집 모델은 아닙니다.
- Z Image — 빠른 시안/아이디어 초안 제작용입니다.

영상/기타
- Seedance 2.0 — 고품질 영상 생성 기본 추천 모델입니다.
- Gemini Omni Flash — 이미지/영상 등 다양한 입력 기반 영상 생성·편집용입니다.
- Multi-Image to 3D — 여러 참고 이미지로 3D/GLB 에셋을 만듭니다.
- Seed Audio 1.0 — 효과음, 분위기음, 짧은 오디오 생성용입니다.
- Virality Predictor — 완성 영상의 후킹/주의집중/바이럴 가능성을 분석합니다.

추천: 이미지 편집은 Nano Banana Pro, 얼굴 유지 합성은 Seedream 4.5, 로고는 Recraft V4.1, 빠른 시안은 Z Image, 배너/텍스트 디자인은 GPT Image 2를 쓰세요.
예상 크레딧은 요청·해상도마다 달라서 실행 전에 Hermes가 자동 계산해 최종 결과에 표시합니다."""


def _parse_csv_set(value: str) -> set[str]:
    return {item.strip() for item in (value or "").split(",") if item.strip()}


def _detect_higgsfield_image_job_type(text: str) -> str:
    """Return a Higgsfield image ``job_type`` implied by Mattermost text."""
    lowered = (text or "").lower()
    for alias, job_type in _HIGGSFIELD_IMAGE_MODEL_ALIASES.items():
        if alias in lowered:
            return job_type
    if "higgsfield" in lowered or "힉스필드" in lowered:
        return "nano_banana_pro"
    return ""


def _is_higgsfield_model_help_request(text: str) -> bool:
    normalized = re.sub(r"\s+", "", (text or "").lower())
    return normalized in {"힉스필드모델", "higgsfield모델", "higgsfieldmodels", "higgsfieldmodel"}


def _looks_like_higgsfield_image_edit_request(text: str, *, has_image: bool) -> bool:
    """Heuristic for the Mattermost-only Higgsfield CLI bypass."""
    lowered = (text or "").lower()
    job_type = _detect_higgsfield_image_job_type(lowered)
    if has_image and job_type:
        return True
    if not has_image and job_type:
        create_terms = ("생성", "제작", "만들", "로고", "아이콘", "logo", "icon", "generate", "create")
        return any(term in lowered for term in create_terms)
    if not has_image:
        return False
    media_terms = ("이미지", "사진", "image", "photo", "첨부")
    edit_terms = ("편집", "수정", "변경", "바꿔", "바꾸", "edit", "change")
    return any(term in lowered for term in media_terms) and any(
        term in lowered for term in edit_terms
    )


def _is_higgsfield_confirmation(text: str) -> bool:
    normalized = re.sub(r"\s+", "", (text or "").lower())
    if not normalized:
        return False
    positives = {
        "진행",
        "진행해",
        "진행해줘",
        "네",
        "응",
        "웅",
        "ㅇㅇ",
        "좋아",
        "확인",
        "제작",
        "제작해줘",
        "생성",
        "생성해줘",
        "실행",
        "실행해줘",
        "바로진행",
        "바로제작",
        "go",
        "yes",
        "y",
        "ok",
    }
    return normalized in positives or normalized.endswith("진행해줘")


def _is_higgsfield_cancel(text: str) -> bool:
    normalized = re.sub(r"\s+", "", (text or "").lower())
    return normalized in {"취소", "취소해", "취소해줘", "아니", "아니야", "no", "n", "cancel"}


def _extract_higgsfield_generation_params(text: str) -> Tuple[str, str]:
    """Extract aspect ratio and resolution from Korean/English request text."""
    raw = text or ""
    aspect = "1:1"
    aspect_match = re.search(r"(?:비율|aspect[_\s-]*ratio)\s*[:：]?\s*(\d{1,2}\s*:\s*\d{1,2})", raw, re.I)
    if aspect_match:
        aspect = aspect_match.group(1).replace(" ", "")
    else:
        generic_aspect = re.search(r"\b(1:1|3:2|2:3|4:3|3:4|4:5|5:4|9:16|16:9|21:9)\b", raw)
        if generic_aspect:
            aspect = generic_aspect.group(1)

    resolution = "2k"
    res_match = re.search(r"(?:해상도|resolution)\s*[:：]?\s*([124])\s*[kK]", raw, re.I)
    if res_match:
        resolution = f"{res_match.group(1)}k"
    else:
        generic_res = re.search(r"\b([124])\s*[kK]\b", raw)
        if generic_res:
            resolution = f"{generic_res.group(1)}k"
    return aspect, resolution


def _build_higgsfield_edit_prompt(text: str, *, use_reference: bool = True) -> str:
    """Build a robust prompt for reference-image clothing/profile edits."""
    user_request = " ".join((text or "").split())
    if use_reference:
        base = (
            "Use the attached image as the primary reference and perform an image-to-image edit, "
            "not a new text-to-image generation. Edit the reference image according to the user's request. Preserve the face, "
            "identity, facial features, expression, skin tone, hairstyle, hair flow, pose, "
            "body proportions, background, lighting, camera angle, and realistic photo "
            "quality unless the user explicitly asks to change them. Do not change the face. "
            "Return a natural professional result."
        )
    else:
        base = (
            "Create the requested image from the user's brief. If this is a logo, icon, or brand graphic, "
            "make it clean, scalable, balanced, and suitable for real brand use. Do not assume there is an attached image reference."
        )
    if user_request:
        return f"{base} User request: {user_request}"
    return base


def _extract_higgsfield_result_url(output: str) -> str:
    """Extract the primary media URL from Higgsfield CLI JSON/text output."""
    raw = (output or "").strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        url_match = re.search(r"https?://\S+", raw)
        return url_match.group(0).rstrip("\"' ,]") if url_match else ""

    def walk(value: Any) -> str:
        if isinstance(value, dict):
            for key in ("result_url", "min_result_url", "url"):
                found = value.get(key)
                if isinstance(found, str) and found.startswith("http"):
                    return found
            for key in ("results", "outputs", "artifacts", "files", "data"):
                found = walk(value.get(key))
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = walk(item)
                if found:
                    return found
        elif isinstance(value, str) and value.startswith("http"):
            return value
        return ""

    return walk(data)


def _extract_higgsfield_job_id(output: str) -> str:
    """Extract a Higgsfield job UUID from CLI output."""
    raw = (output or "").strip()
    uuid_pattern = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(uuid_pattern, raw)
        return match.group(0) if match else ""

    def walk(value: Any) -> str:
        if isinstance(value, dict):
            for key in ("id", "job_id", "jobId"):
                found = value.get(key)
                if isinstance(found, str) and re.fullmatch(uuid_pattern, found):
                    return found
            for nested in value.values():
                found = walk(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = walk(item)
                if found:
                    return found
        elif isinstance(value, str):
            match = re.search(uuid_pattern, value)
            if match:
                return match.group(0)
        return ""

    return walk(data)


def _with_mentions_disabled(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a post payload that prevents Mattermost from firing mentions."""
    props = payload.get("props")
    if isinstance(props, dict):
        payload["props"] = {**props, **_MATTERMOST_DISABLE_MENTIONS_PROPS}
    else:
        payload["props"] = dict(_MATTERMOST_DISABLE_MENTIONS_PROPS)
    return payload


def check_mattermost_requirements() -> bool:
    """Return True if the Mattermost adapter runtime dependency is available."""
    try:
        import aiohttp  # noqa: F401
        return True
    except ImportError:
        logger.warning("Mattermost: aiohttp not installed")
        return False


def validate_mattermost_config(config: PlatformConfig) -> bool:
    """Return True when Mattermost has enough config to connect."""
    extra = getattr(config, "extra", {}) or {}
    token = (getattr(config, "token", None) or os.getenv("MATTERMOST_TOKEN", "")).strip()
    url = (extra.get("url", "") or os.getenv("MATTERMOST_URL", "")).strip()
    if not token:
        logger.debug("Mattermost: MATTERMOST_TOKEN not set")
        return False
    if not url:
        logger.warning("Mattermost: MATTERMOST_URL not set")
        return False
    return True


class MattermostAdapter(BasePlatformAdapter):
    """Gateway adapter for Mattermost (self-hosted or cloud)."""

    splits_long_messages = True  # send() chunks via truncate_message(MAX_POST_LENGTH)

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.MATTERMOST)

        self._base_url: str = (
            config.extra.get("url", "")
            or os.getenv("MATTERMOST_URL", "")
        ).rstrip("/")
        self._token: str = config.token or os.getenv("MATTERMOST_TOKEN", "")

        self._bot_user_id: str = ""
        self._bot_username: str = ""

        # aiohttp session + websocket handle
        self._session: Any = None  # aiohttp.ClientSession
        self._ws: Any = None       # aiohttp.ClientWebSocketResponse
        self._ws_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._closing = False

        # Reply mode: "thread" to nest replies, "off" for flat messages.
        self._reply_mode: str = (
            config.extra.get("reply_mode", "")
            or os.getenv("MATTERMOST_REPLY_MODE", "off")
        ).lower()

        self._last_post_status: Optional[int] = None
        self._last_post_error: str = ""

        # Dedup cache (prevent reprocessing)
        self._dedup = MessageDeduplicator()

        # Mattermost-only Higgsfield CLI bypass state.  Keyed by
        # ``channel_id:sender_id`` so a user can confirm the credit estimate
        # without the request going through the LLM/session cache.
        self._pending_higgsfield_edits: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def _api_get(self, path: str) -> Dict[str, Any]:
        """GET /api/v4/{path}."""
        import aiohttp
        if ".." in path:
            logger.error("MM API path traversal blocked: %s", path)
            return {}
        url = f"{self._base_url}/api/v4/{path.lstrip('/')}"
        try:
            async with self._session.get(url, headers=self._headers(), timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.error("MM API GET %s → %s: %s", path, resp.status, body[:200])
                    return {}
                return await resp.json()
        except aiohttp.ClientError as exc:
            logger.error("MM API GET %s network error: %s", path, exc)
            return {}

    async def _api_post(
        self, path: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """POST /api/v4/{path} with JSON body."""
        import aiohttp
        if ".." in path:
            logger.error("MM API path traversal blocked: %s", path)
            return {}
        url = f"{self._base_url}/api/v4/{path.lstrip('/')}"
        self._last_post_status = None
        self._last_post_error = ""
        try:
            async with self._session.post(
                url, headers=self._headers(), json=payload,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                self._last_post_status = resp.status
                if resp.status >= 400:
                    body = await resp.text()
                    self._last_post_error = body or ""
                    logger.error("MM API POST %s → %s: %s", path, resp.status, body[:200])
                    return {}
                return await resp.json()
        except aiohttp.ClientError as exc:
            self._last_post_error = str(exc)
            logger.error("MM API POST %s network error: %s", path, exc)
            return {}

    async def _thread_root_for_send(
        self,
        reply_to: Optional[str],
        metadata: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        """Resolve the Mattermost root_id from reply_to or metadata."""
        if self._reply_mode != "thread":
            return None
        candidate = reply_to
        if not candidate and isinstance(metadata, dict):
            candidate = metadata.get("thread_id") or metadata.get("root_id")
        if not candidate:
            return None
        return await self._resolve_root_id(str(candidate))

    def _last_post_failure_is_broken_thread_root(self) -> bool:
        """Return True only for clear invalid/missing Mattermost thread roots."""
        if self._last_post_status not in {400, 404}:
            return False
        body = (self._last_post_error or "").lower()
        if not body:
            return False
        rootish = any(marker in body for marker in ("root_id", "rootid", "root id", "thread", "post"))
        broken = any(marker in body for marker in ("invalid", "not found", "does not exist", "missing"))
        return rootish and broken

    async def _post_preserving_thread(
        self,
        chat_id: str,
        payload: Dict[str, Any],
        metadata: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Post once, optionally falling back flat for final notify content."""
        data = await self._api_post("posts", payload)
        if data or "root_id" not in payload:
            return data
        if not (isinstance(metadata, dict) and metadata.get("notify")):
            return data
        if not self._last_post_failure_is_broken_thread_root():
            return data

        flat_payload = dict(payload)
        flat_payload.pop("root_id", None)
        original = str(flat_payload.get("message") or "")
        flat_payload["message"] = (
            "⚠️ Mattermost thread delivery failed; posting final reply in channel.\n\n"
            + original
        ).strip()
        logger.warning(
            "Mattermost: falling back to flat channel delivery for notify-worthy post in %s",
            chat_id,
        )
        return await self._api_post("posts", flat_payload)

    async def _api_put(
        self, path: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """PUT /api/v4/{path} with JSON body."""
        import aiohttp
        if ".." in path:
            logger.error("MM API path traversal blocked: %s", path)
            return {}
        url = f"{self._base_url}/api/v4/{path.lstrip('/')}"
        try:
            async with self._session.put(
                url, headers=self._headers(), json=payload
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.error("MM API PUT %s → %s: %s", path, resp.status, body[:200])
                    return {}
                return await resp.json()
        except aiohttp.ClientError as exc:
            logger.error("MM API PUT %s network error: %s", path, exc)
            return {}

    async def _upload_file(
        self, channel_id: str, file_data: bytes, filename: str, content_type: str = "application/octet-stream"
    ) -> Optional[str]:
        """Upload a file and return its file ID, or None on failure."""
        import aiohttp

        url = f"{self._base_url}/api/v4/files"
        form = aiohttp.FormData()
        form.add_field("channel_id", channel_id)
        form.add_field(
            "files",
            file_data,
            filename=filename,
            content_type=content_type,
        )
        headers = {"Authorization": f"Bearer {self._token}"}
        async with self._session.post(url, headers=headers, data=form, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status >= 400:
                body = await resp.text()
                logger.error("MM file upload → %s: %s", resp.status, body[:200])
                return None
            data = await resp.json()
            infos = data.get("file_infos", [])
            return infos[0]["id"] if infos else None

    # ------------------------------------------------------------------
    # Required overrides
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to Mattermost and start the WebSocket listener."""
        import aiohttp

        if not self._base_url or not self._token:
            logger.error("Mattermost: URL or token not configured")
            return False

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )
        self._closing = False

        # Verify credentials and fetch bot identity.
        me = await self._api_get("users/me")
        if not me or "id" not in me:
            logger.error("Mattermost: failed to authenticate — check MATTERMOST_TOKEN and MATTERMOST_URL")
            await self._session.close()
            return False

        self._bot_user_id = me["id"]
        self._bot_username = me.get("username", "")
        logger.info(
            "Mattermost: authenticated as @%s (%s) on %s",
            self._bot_username,
            self._bot_user_id,
            self._base_url,
        )

        # Start WebSocket in background.
        self._ws_task = asyncio.create_task(self._ws_loop())
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        """Disconnect from Mattermost."""
        self._closing = True

        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()

        if self._ws:
            await self._ws.close()
            self._ws = None

        if self._session and not self._session.closed:
            await self._session.close()

        logger.info("Mattermost: disconnected")


    async def _resolve_root_id(self, post_id: str) -> str:
        """Resolve a post_id to the thread root_id for Mattermost.

        Mattermost requires root_id to be the *root* post of a thread.
        If the post is a reply (has its own root_id), we must use that
        root_id instead.  Using a reply's own ID as root_id causes
        "Invalid RootId parameter" errors.
        """
        if not post_id:
            return post_id
        # Check if this post has a root_id (meaning it's a reply)
        data = await self._api_get(f"posts/{post_id}")
        if data and data.get("root_id"):
            return data["root_id"]
        return post_id

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a message (or multiple chunks) to a channel."""
        if not content:
            return SendResult(success=True)

        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted, MAX_POST_LENGTH)

        last_id = None
        for chunk in chunks:
            payload: Dict[str, Any] = _with_mentions_disabled({
                "channel_id": chat_id,
                "message": chunk,
            })
            # Thread support: reply_to or metadata["thread_id"] is the root post ID.
            resolved_root = await self._thread_root_for_send(reply_to, metadata)
            if resolved_root:
                payload["root_id"] = resolved_root

            data = await self._post_preserving_thread(chat_id, payload, metadata)
            if not data or "id" not in data:
                return SendResult(success=False, error="Failed to create post")
            last_id = data["id"]

        return SendResult(success=True, message_id=last_id)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return channel name and type."""
        data = await self._api_get(f"channels/{chat_id}")
        if not data:
            return {"name": chat_id, "type": "channel"}

        ch_type = _CHANNEL_TYPE_MAP.get(data.get("type", "O"), "channel")
        display_name = data.get("display_name") or data.get("name") or chat_id
        return {"name": display_name, "type": ch_type}

    # ------------------------------------------------------------------
    # Mattermost Higgsfield image-edit bypass
    # ------------------------------------------------------------------

    def _higgsfield_pending_key(self, channel_id: str, sender_id: str) -> str:
        return f"{channel_id}:{sender_id}"

    async def _run_higgsfield_cli(self, args: List[str], *, timeout: float = 600.0) -> Tuple[int, str, str]:
        """Run the Higgsfield CLI without exposing secrets to the LLM."""
        exe = shutil.which("higgsfield")
        if not exe:
            return 127, "", "higgsfield CLI not found on PATH"

        create_kwargs: Dict[str, Any] = {}
        if os.name == "nt":
            create_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        proc = await asyncio.create_subprocess_exec(
            exe,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **create_kwargs,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return 124, "", f"higgsfield command timed out after {timeout:.0f}s"
        return (
            proc.returncode or 0,
            stdout_b.decode("utf-8", "replace"),
            stderr_b.decode("utf-8", "replace"),
        )

    async def _maybe_start_higgsfield_image_edit(
        self,
        *,
        channel_id: str,
        sender_id: str,
        post_id: str,
        message_text: str,
        media_urls: List[str],
        media_types: List[str],
        force: bool = False,
    ) -> bool:
        """Intercept image-edit requests and launch Higgsfield directly.

        Returns True when the message was handled and should not enter the
        normal LLM path.
        """
        image_paths = [
            path for path, mime in zip(media_urls, media_types)
            if mime.startswith("image/") and path
        ]
        if not force and not _looks_like_higgsfield_image_edit_request(message_text, has_image=bool(image_paths)):
            return False

        job_type = _detect_higgsfield_image_job_type(message_text) or "nano_banana_pro"
        aspect, resolution = _extract_higgsfield_generation_params(message_text)
        accepts_image_reference = job_type in _HIGGSFIELD_IMAGE_REFERENCE_JOB_TYPES
        image_path = image_paths[0] if image_paths and accepts_image_reference else ""
        prompt = _build_higgsfield_edit_prompt(message_text, use_reference=bool(image_path))

        cost_args = [
            "generate", "cost", job_type,
            "--prompt", prompt,
            "--aspect_ratio", aspect,
        ]
        if image_path:
            cost_args.extend(["--image-references", image_path])
        if job_type in _HIGGSFIELD_RESOLUTION_JOB_TYPES:
            cost_args.extend(["--resolution", resolution])
        if job_type in {"seedream_v4_5", "seedream_v5_lite"} and resolution != "1k":
            cost_args.extend(["--quality", "high"])
        if job_type == "recraft_v4_1":
            cost_args.extend(["--model_type", "vector"])
        code, stdout, stderr = await self._run_higgsfield_cli(cost_args, timeout=120)
        if code != 0:
            logger.warning("Mattermost Higgsfield cost failed: %s", (stderr or stdout)[:500])
            cost = "확인 불가"
        else:
            cost = " ".join(stdout.strip().split()) or "확인 불가"

        pending = {
            "job_type": job_type,
            "prompt": prompt,
            "image_path": image_path,
            "aspect": aspect,
            "resolution": resolution,
            "cost": cost,
            "reply_to": post_id,
        }
        asyncio.create_task(
            self._run_pending_higgsfield_edit(channel_id, post_id, pending)
        )
        return True

    def _is_higgsfield_image_edit_channel(self, channel_id: str) -> bool:
        configured = _parse_csv_set(os.getenv("HIGGSFIELD_IMAGE_EDIT_CHANNELS", ""))
        return bool(channel_id and channel_id in configured)

    async def _maybe_finish_pending_higgsfield_edit(
        self,
        *,
        channel_id: str,
        sender_id: str,
        post_id: str,
        message_text: str,
    ) -> bool:
        key = self._higgsfield_pending_key(channel_id, sender_id)
        pending = self._pending_higgsfield_edits.get(key)
        if not pending:
            return False

        if _is_higgsfield_cancel(message_text):
            self._pending_higgsfield_edits.pop(key, None)
            await self.send(channel_id, "Higgsfield 이미지 편집을 취소했습니다.")
            return True
        if not _is_higgsfield_confirmation(message_text):
            return False

        self._pending_higgsfield_edits.pop(key, None)
        await self.send(
            channel_id,
            "Higgsfield 이미지 편집을 시작했습니다. 완료되면 결과 이미지를 이 채널에 올리겠습니다.",
        )
        asyncio.create_task(
            self._run_pending_higgsfield_edit(channel_id, "", pending)
        )
        return True

    async def _run_pending_higgsfield_edit(
        self,
        channel_id: str,
        reply_to: str,
        pending: Dict[str, Any],
    ) -> None:
        create_args = [
            "generate", "create", pending["job_type"],
            "--prompt", pending["prompt"],
            "--aspect_ratio", pending["aspect"],
            "--wait",
            "--wait-timeout", "20m",
            "--wait-interval", "5s",
            "--json",
        ]
        if pending.get("image_path"):
            create_args.extend(["--image-references", pending["image_path"]])
        job_type = pending["job_type"]
        if job_type in _HIGGSFIELD_RESOLUTION_JOB_TYPES:
            create_args.extend(["--resolution", pending["resolution"]])
        if job_type in {"seedream_v4_5", "seedream_v5_lite"} and pending["resolution"] != "1k":
            create_args.extend(["--quality", "high"])
        if job_type == "recraft_v4_1":
            create_args.extend(["--model_type", "vector"])
        code, stdout, stderr = await self._run_higgsfield_cli(create_args, timeout=1500)
        if code != 0:
            logger.warning("Mattermost Higgsfield create failed: %s", (stderr or stdout)[:500])
            await self.send(
                channel_id,
                "Higgsfield 이미지 편집 실행에 실패했습니다.\n"
                f"오류: {(stderr or stdout or 'unknown error')[:1000]}",
            )
            return

        result_url = _extract_higgsfield_result_url(stdout)
        if not result_url:
            job_id = _extract_higgsfield_job_id(stdout)
            if job_id:
                result_url = await self._resolve_higgsfield_result_url(job_id)

        caption = (
            "Higgsfield 이미지 편집이 완료되었습니다.\n"
            f"- 모델: {pending['job_type']}\n"
            f"- 비용: {pending.get('cost', '확인 불가')}\n"
            f"- 비율/해상도: {pending['aspect']} / {str(pending['resolution']).upper()}"
        )
        if result_url:
            await self.send_image(channel_id, result_url, caption=caption, reply_to=reply_to)
        else:
            await self.send(
                channel_id,
                caption + "\n\n결과 URL을 자동으로 찾지 못했습니다. 원본 출력:\n"
                f"```text\n{stdout[:1200]}\n```",
                reply_to=reply_to,
            )

    async def _resolve_higgsfield_result_url(self, job_id: str) -> str:
        """Resolve a Higgsfield job UUID to a final media URL, retrying briefly.

        Some `higgsfield generate create --wait --json` runs print only the job
        UUID, and an immediate `generate get` can briefly return before
        `result_url` is populated. Polling avoids surfacing the UUID-only output
        to Mattermost users as a false failure.
        """
        last_error = ""
        for attempt in range(6):
            get_code, get_stdout, get_stderr = await self._run_higgsfield_cli(
                ["generate", "get", job_id, "--json"], timeout=120
            )
            if get_code == 0:
                result_url = _extract_higgsfield_result_url(get_stdout)
                if result_url:
                    return result_url
                last_error = get_stdout[:500]
            else:
                last_error = (get_stderr or get_stdout)[:500]
                logger.warning(
                    "Mattermost Higgsfield get failed for %s: %s",
                    job_id,
                    last_error,
                )

            if attempt < 5:
                await asyncio.sleep(5)

        wait_code, wait_stdout, wait_stderr = await self._run_higgsfield_cli(
            ["generate", "wait", job_id, "--json"], timeout=600
        )
        if wait_code == 0:
            result_url = _extract_higgsfield_result_url(wait_stdout)
            if result_url:
                return result_url
        logger.warning(
            "Mattermost Higgsfield URL unresolved for %s: %s",
            job_id,
            (wait_stderr or wait_stdout or last_error)[:500],
        )
        return ""

    # ------------------------------------------------------------------
    # Optional overrides
    # ------------------------------------------------------------------

    async def send_typing(
        self, chat_id: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Send a typing indicator."""
        await self._api_post(
            f"users/{self._bot_user_id}/typing",
            {"channel_id": chat_id},
        )

    async def edit_message(
        self, chat_id: str, message_id: str, content: str, *, finalize: bool = False
    ) -> SendResult:
        """Edit an existing post."""
        formatted = self.format_message(content)
        data = await self._api_put(
            f"posts/{message_id}/patch",
            _with_mentions_disabled({"message": formatted}),
        )
        if not data or "id" not in data:
            return SendResult(success=False, error="Failed to edit post")
        return SendResult(success=True, message_id=data["id"])

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Download an image and upload it as a file attachment."""
        return await self._send_url_as_file(
            chat_id, image_url, caption, reply_to, "image", metadata
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload a local image file."""
        return await self._send_local_file(
            chat_id, image_path, caption, reply_to, metadata=metadata
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload a local file as a document."""
        return await self._send_local_file(
            chat_id, file_path, caption, reply_to, file_name, metadata
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload an audio file."""
        return await self._send_local_file(
            chat_id, audio_path, caption, reply_to, metadata=metadata
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload a video file."""
        return await self._send_local_file(
            chat_id, video_path, caption, reply_to, metadata=metadata
        )

    def format_message(self, content: str) -> str:
        """Mattermost uses standard Markdown — mostly pass through.

        Strip image markdown into plain links (files are uploaded separately).
        """
        # Convert ![alt](url) to just the URL — Mattermost renders
        # image URLs as inline previews automatically.
        content = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\2", content)
        return content

    # ------------------------------------------------------------------
    # File helpers
    # ------------------------------------------------------------------

    async def _send_url_as_file(
        self,
        chat_id: str,
        url: str,
        caption: Optional[str],
        reply_to: Optional[str],
        kind: str = "file",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Download a URL and upload it as a file attachment."""
        from tools.url_safety import is_safe_url
        if not is_safe_url(url):
            logger.warning("Mattermost: blocked unsafe URL (SSRF protection)")
            return await self.send(chat_id, f"{caption or ''}\n{url}".strip(), reply_to, metadata=metadata)

        import aiohttp

        file_data = None
        ct = "application/octet-stream"
        fname = url.rsplit("/", 1)[-1].split("?")[0] or f"{kind}.png"

        for attempt in range(3):
            try:
                async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status >= 500 or resp.status == 429:
                        if attempt < 2:
                            logger.debug("Mattermost download retry %d/2 for %s (status %d)",
                                         attempt + 1, url[:80], resp.status)
                            await asyncio.sleep(1.5 * (attempt + 1))
                            continue
                    if resp.status >= 400:
                        return await self.send(chat_id, f"{caption or ''}\n{url}".strip(), reply_to, metadata=metadata)
                    file_data = await resp.read()
                    ct = resp.content_type or "application/octet-stream"
                    break
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                logger.warning("Mattermost: failed to download %s after %d attempts: %s", url, attempt + 1, exc)
                return await self.send(chat_id, f"{caption or ''}\n{url}".strip(), reply_to, metadata=metadata)

        if file_data is None:
            logger.warning("Mattermost: download returned no data for %s", url)
            return await self.send(chat_id, f"{caption or ''}\n{url}".strip(), reply_to, metadata=metadata)

        file_id = await self._upload_file(chat_id, file_data, fname, ct)
        if not file_id:
            return await self.send(chat_id, f"{caption or ''}\n{url}".strip(), reply_to, metadata=metadata)

        payload: Dict[str, Any] = _with_mentions_disabled({
            "channel_id": chat_id,
            "message": caption or "",
            "file_ids": [file_id],
        })
        resolved_root = await self._thread_root_for_send(reply_to, metadata)
        if resolved_root:
            payload["root_id"] = resolved_root

        data = await self._post_preserving_thread(chat_id, payload, metadata)
        if not data or "id" not in data:
            return SendResult(success=False, error="Failed to post with file")
        return SendResult(success=True, message_id=data["id"])

    async def _send_local_file(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str],
        reply_to: Optional[str],
        file_name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload a local file and attach it to a post."""
        import mimetypes

        p = Path(file_path)
        if not p.exists():
            logger.warning(
                "Mattermost: local file not found, skipping: %s", file_path
            )
            return SendResult(success=True, message_id=None)

        fname = file_name or p.name
        ct = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        file_data = p.read_bytes()

        file_id = await self._upload_file(chat_id, file_data, fname, ct)
        if not file_id:
            return SendResult(success=False, error="File upload failed")

        payload: Dict[str, Any] = _with_mentions_disabled({
            "channel_id": chat_id,
            "message": caption or "",
            "file_ids": [file_id],
        })
        resolved_root = await self._thread_root_for_send(reply_to, metadata)
        if resolved_root:
            payload["root_id"] = resolved_root

        data = await self._post_preserving_thread(chat_id, payload, metadata)
        if not data or "id" not in data:
            return SendResult(success=False, error="Failed to post with file")
        return SendResult(success=True, message_id=data["id"])

    async def send_multiple_images(
        self,
        chat_id: str,
        images: List[Tuple[str, str]],
        metadata: Optional[Dict[str, Any]] = None,
        human_delay: float = 0.0,
    ) -> None:
        """Send a batch of images as a single Mattermost post with multiple attachments.

        Mattermost supports up to 5 ``file_ids`` per post. Each image is
        uploaded individually (Mattermost's file API is one-at-a-time),
        then a single post is created referencing all uploaded file_ids
        at once. Batches larger than 5 are chunked. Falls back to the
        base per-image loop on total failure.
        """
        if not images:
            return

        import mimetypes
        import aiohttp
        from urllib.parse import unquote as _unquote

        CHUNK = 5  # Mattermost post file_ids cap
        chunks = [images[i:i + CHUNK] for i in range(0, len(images), CHUNK)]

        for chunk_idx, chunk in enumerate(chunks):
            if human_delay > 0 and chunk_idx > 0:
                await asyncio.sleep(human_delay)

            file_ids: List[str] = []
            caption_parts: List[str] = []
            try:
                for image_url, alt_text in chunk:
                    if alt_text:
                        caption_parts.append(alt_text)

                    if image_url.startswith("file://"):
                        local_path = _unquote(image_url[7:])
                        p = Path(local_path)
                        if not p.exists():
                            logger.warning("Mattermost: skipping missing image %s", local_path)
                            continue
                        fname = p.name
                        ct = mimetypes.guess_type(fname)[0] or "image/png"
                        file_data = p.read_bytes()
                    else:
                        from tools.url_safety import is_safe_url
                        if not is_safe_url(image_url):
                            logger.warning("Mattermost: blocked unsafe image URL in batch")
                            continue
                        try:
                            async with self._session.get(
                                image_url, timeout=aiohttp.ClientTimeout(total=30)
                            ) as resp:
                                if resp.status >= 400:
                                    logger.warning(
                                        "Mattermost: failed to download image (HTTP %d): %s",
                                        resp.status, image_url[:80],
                                    )
                                    continue
                                file_data = await resp.read()
                                ct = resp.content_type or "image/png"
                        except Exception as dl_err:
                            logger.warning("Mattermost: download failed for %s: %s", image_url[:80], dl_err)
                            continue
                        fname = image_url.rsplit("/", 1)[-1].split("?")[0] or f"image_{len(file_ids)}.png"

                    fid = await self._upload_file(chat_id, file_data, fname, ct)
                    if fid:
                        file_ids.append(fid)

                if not file_ids:
                    continue

                payload: Dict[str, Any] = _with_mentions_disabled({
                    "channel_id": chat_id,
                    "message": "\n".join(caption_parts),
                    "file_ids": file_ids,
                })
                resolved_root = await self._thread_root_for_send(None, metadata)
                if resolved_root:
                    payload["root_id"] = resolved_root
                logger.info(
                    "Mattermost: sending %d image(s) as single post (chunk %d/%d)",
                    len(file_ids), chunk_idx + 1, len(chunks),
                )
                data = await self._post_preserving_thread(chat_id, payload, metadata)
                if not data or "id" not in data:
                    logger.warning("Mattermost: multi-image post failed, falling back")
                    await super().send_multiple_images(chat_id, chunk, metadata, human_delay=human_delay)
            except Exception as e:
                logger.warning(
                    "Mattermost: multi-image send failed (chunk %d/%d), falling back: %s",
                    chunk_idx + 1, len(chunks), e, exc_info=True,
                )
                await super().send_multiple_images(chat_id, chunk, metadata, human_delay=human_delay)

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    async def _ws_loop(self) -> None:
        """Connect to the WebSocket and listen for events, reconnecting on failure."""
        delay = _RECONNECT_BASE_DELAY
        while not self._closing:
            try:
                await self._ws_connect_and_listen()
                # Clean disconnect — reset delay.
                delay = _RECONNECT_BASE_DELAY
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if self._closing:
                    return
                # Detect permanent auth/permission failures that will never
                # succeed on retry — stop reconnecting instead of looping forever.
                import aiohttp
                err_str = str(exc).lower()
                if isinstance(exc, aiohttp.WSServerHandshakeError) and exc.status in {401, 403}:
                    logger.error("Mattermost WS auth failed (HTTP %d) — stopping reconnect", exc.status)
                    return
                if "401" in err_str or "403" in err_str or "unauthorized" in err_str:
                    logger.error("Mattermost WS permanent error: %s — stopping reconnect", exc)
                    return
                logger.warning("Mattermost WS error: %s — reconnecting in %.0fs", exc, delay)

            if self._closing:
                return

            # Exponential backoff with jitter.
            import random
            jitter = delay * _RECONNECT_JITTER * random.random()
            await asyncio.sleep(delay + jitter)
            delay = min(delay * 2, _RECONNECT_MAX_DELAY)

    async def _ws_connect_and_listen(self) -> None:
        """Single WebSocket session: connect, authenticate, process events."""
        # Build WS URL: https:// → wss://, http:// → ws://
        ws_url = re.sub(r"^http", "ws", self._base_url) + "/api/v4/websocket"
        logger.info("Mattermost: connecting to %s", ws_url)

        self._ws = await self._session.ws_connect(ws_url, heartbeat=30.0)

        # Authenticate via the WebSocket.
        auth_msg = {
            "seq": 1,
            "action": "authentication_challenge",
            "data": {"token": self._token},
        }
        await self._ws.send_json(auth_msg)
        logger.info("Mattermost: WebSocket connected and authenticated")

        async for raw_msg in self._ws:
            if self._closing:
                return

            if raw_msg.type in {
                raw_msg.type.TEXT,
                raw_msg.type.BINARY,
            }:
                try:
                    event = json.loads(raw_msg.data)
                except (json.JSONDecodeError, TypeError):
                    continue
                await self._handle_ws_event(event)
            elif raw_msg.type in {
                raw_msg.type.ERROR,
                raw_msg.type.CLOSE,
                raw_msg.type.CLOSING,
                raw_msg.type.CLOSED,
            }:
                logger.info("Mattermost: WebSocket closed (%s)", raw_msg.type)
                break

    async def _handle_ws_event(self, event: Dict[str, Any]) -> None:
        """Process a single WebSocket event."""
        event_type = event.get("event")
        if event_type != "posted":
            return

        data = event.get("data", {})
        raw_post_str = data.get("post")
        if not raw_post_str:
            return

        try:
            post = json.loads(raw_post_str)
        except (json.JSONDecodeError, TypeError):
            return

        # Ignore own messages.
        if post.get("user_id") == self._bot_user_id:
            return

        # Ignore system posts.
        if post.get("type"):
            return

        post_id = post.get("id", "")

        # Dedup.
        if self._dedup.is_duplicate(post_id):
            return

        # Build message event.
        channel_id = post.get("channel_id", "")
        channel_type_raw = data.get("channel_type", "O")
        chat_type = _CHANNEL_TYPE_MAP.get(channel_type_raw, "channel")

        # For DMs, user_id is sufficient.  For channels, check for @mention.
        message_text = post.get("message", "")

        # Mention-gating for non-DM channels.
        # Config (config.yaml `mattermost.*` with env-var fallback):
        #   require_mention / MATTERMOST_REQUIRE_MENTION: Require @mention in channels (default: true)
        #   free_response_channels / MATTERMOST_FREE_RESPONSE_CHANNELS: Channel IDs where bot responds without mention
        #   allowed_channels / MATTERMOST_ALLOWED_CHANNELS: If set, bot ONLY responds in these channels (whitelist)
        if channel_type_raw != "D":
            # allowed_channels check (whitelist — must pass before other gating).
            # When set, messages from channels NOT in this list are silently
            # ignored, even if @mentioned.  DMs are already excluded above.
            allowed_raw = self.config.extra.get("allowed_channels") if self.config.extra else None
            if allowed_raw is None:
                allowed_raw = os.getenv("MATTERMOST_ALLOWED_CHANNELS", "")
            if isinstance(allowed_raw, list):
                allowed_channels = {str(c).strip() for c in allowed_raw if str(c).strip()}
            else:
                allowed_channels = {
                    c.strip() for c in str(allowed_raw).split(",") if c.strip()
                }
            if allowed_channels and channel_id not in allowed_channels:
                logger.debug(
                    "Mattermost: ignoring message in non-allowed channel: %s",
                    channel_id,
                )
                return

            require_mention = os.getenv(
                "MATTERMOST_REQUIRE_MENTION", "true"
            ).lower() not in {"false", "0", "no"}

            free_channels_raw = os.getenv("MATTERMOST_FREE_RESPONSE_CHANNELS", "")
            free_channels = {ch.strip() for ch in free_channels_raw.split(",") if ch.strip()}
            is_free_channel = channel_id in free_channels

            mention_patterns = [
                f"@{self._bot_username}",
                f"@{self._bot_user_id}",
            ]
            has_mention = any(
                pattern.lower() in message_text.lower()
                for pattern in mention_patterns
            )

            if require_mention and not is_free_channel and not has_mention:
                logger.debug(
                    "Mattermost: skipping non-DM message without @mention (channel=%s)",
                    channel_id,
                )
                return

            # Strip @mention from the message text so the agent sees clean input.
            if has_mention:
                for pattern in mention_patterns:
                    message_text = re.sub(
                        re.escape(pattern), "", message_text, flags=re.IGNORECASE
                    ).strip()

        # Resolve sender info.
        sender_id = post.get("user_id", "")
        sender_name = data.get("sender_name", "").lstrip("@") or sender_id

        # Thread support: if the post is in a thread, use root_id. In
        # thread mode, top-level channel posts are valid roots for progress.
        thread_id = post.get("root_id") or None
        if (
            not thread_id
            and self._reply_mode == "thread"
            and channel_type_raw != "D"
            and post_id
        ):
            thread_id = post_id

        # Determine message type.
        file_ids = post.get("file_ids") or []
        msg_type = MessageType.TEXT
        if message_text[:1].isspace() and message_text.lstrip().startswith("/"):
            message_text = message_text.lstrip()
        if message_text.startswith("/"):
            msg_type = MessageType.COMMAND

        if msg_type != MessageType.COMMAND and _is_higgsfield_model_help_request(message_text):
            await self.send(channel_id, _HIGGSFIELD_MODEL_HELP_TEXT, reply_to=post_id)
            return

        # Download file attachments immediately (URLs require auth headers
        # that downstream tools won't have).
        media_urls: List[str] = []
        media_types: List[str] = []
        for fid in file_ids:
            try:
                file_info = await self._api_get(f"files/{fid}/info")
                fname = file_info.get("name", f"file_{fid}")
                ext = Path(fname).suffix or ""
                mime = file_info.get("mime_type", "application/octet-stream")

                import aiohttp
                dl_url = f"{self._base_url}/api/v4/files/{fid}"
                async with self._session.get(
                    dl_url,
                    headers={"Authorization": f"Bearer {self._token}"},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status < 400:
                        file_data = await resp.read()
                        from gateway.platforms.base import cache_image_from_bytes, cache_document_from_bytes
                        if mime.startswith("image/"):
                            local_path = cache_image_from_bytes(file_data, ext or ".png")
                            media_urls.append(local_path)
                            media_types.append(mime)
                        elif mime.startswith("audio/"):
                            from gateway.platforms.base import cache_audio_from_bytes
                            local_path = cache_audio_from_bytes(file_data, ext or ".ogg")
                            media_urls.append(local_path)
                            media_types.append(mime)
                        else:
                            local_path = cache_document_from_bytes(file_data, fname)
                            media_urls.append(local_path)
                            media_types.append(mime)
                    else:
                        logger.warning("Mattermost: failed to download file %s: HTTP %s", fid, resp.status)
            except Exception as exc:
                logger.warning("Mattermost: error downloading file %s: %s", fid, exc)

        # Set message type based on downloaded media types.
        if media_types and msg_type == MessageType.TEXT:
            if any(m.startswith("image/") for m in media_types):
                msg_type = MessageType.PHOTO
            elif any(m.startswith("audio/") for m in media_types):
                msg_type = MessageType.VOICE
            elif media_types:
                msg_type = MessageType.DOCUMENT

        # Mattermost-specific Higgsfield image edit bypass.  This runs before
        # the normal LLM path so stale cached sessions cannot respond with
        # "I'll prepare a prompt" when a directly executable Higgsfield CLI
        # path is available.
        if msg_type != MessageType.COMMAND:
            if await self._maybe_finish_pending_higgsfield_edit(
                channel_id=channel_id,
                sender_id=sender_id,
                post_id=post_id,
                message_text=message_text,
            ):
                return

            if await self._maybe_start_higgsfield_image_edit(
                channel_id=channel_id,
                sender_id=sender_id,
                post_id=post_id,
                message_text=message_text,
                media_urls=media_urls,
                media_types=media_types,
                force=self._is_higgsfield_image_edit_channel(channel_id) and any(m.startswith("image/") for m in media_types),
            ):
                return

        source = self.build_source(
            chat_id=channel_id,
            chat_type=chat_type,
            user_id=sender_id,
            user_name=sender_name,
            thread_id=thread_id,
            message_id=post_id,
        )

        # Per-channel ephemeral prompt
        from gateway.platforms.base import resolve_channel_prompt
        _channel_prompt = resolve_channel_prompt(
            self.config.extra, channel_id, None,
        )

        msg_event = MessageEvent(
            text=message_text,
            message_type=msg_type,
            source=source,
            raw_message=post,
            message_id=post_id,
            media_urls=media_urls if media_urls else None,
            media_types=media_types if media_types else None,
            channel_prompt=_channel_prompt,
        )

        await self.handle_message(msg_event)




# ---------------------------------------------------------------------------
# Plugin standalone-send (out-of-process cron delivery via Mattermost REST)
# ---------------------------------------------------------------------------


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[list] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Send via the Mattermost v4 REST API without a live gateway adapter.

    Used by ``tools/send_message_tool._send_via_adapter`` when the gateway
    runner is not in this process (typical for cron jobs running out-of-process).
    Reads ``MATTERMOST_TOKEN`` from ``pconfig.token`` (set by the gateway
    config loader from env) and falls back to the ``MATTERMOST_TOKEN`` env
    var.  Server URL comes from ``pconfig.extra["url"]`` (set by the YAML
    bridge / env loader) or the ``MATTERMOST_URL`` env var.

    Thread replies (Mattermost CRT) are supported via the ``root_id`` field
    on the ``POST /posts`` payload — pass ``thread_id`` when threading is
    desired.  ``media_files`` are uploaded via ``POST /files``
    (multipart/form-data), then their returned ``file_id`` values are
    attached to the post.

    ``force_document`` is accepted for signature parity with other
    standalone senders but unused — Mattermost stores every uploaded file
    as a generic attachment regardless.
    """
    try:
        import aiohttp
    except ImportError:
        return {"error": "aiohttp not installed. Run: pip install aiohttp"}

    base_url = (
        (getattr(pconfig, "extra", {}) or {}).get("url")
        or os.getenv("MATTERMOST_URL", "")
    ).rstrip("/")
    token = (getattr(pconfig, "token", None) or os.getenv("MATTERMOST_TOKEN", "")).strip()
    if not base_url or not token:
        return {
            "error": (
                "Mattermost standalone send: MATTERMOST_URL and "
                "MATTERMOST_TOKEN must both be set"
            )
        }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    upload_headers = {"Authorization": f"Bearer {token}"}

    media_files = media_files or []

    try:
        # Resolve proxy + session kwargs once so a single ClientSession can
        # cover the optional file uploads + final post.
        from gateway.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp
        _proxy = resolve_proxy_url(platform_env_var="MATTERMOST_PROXY")
        _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60),
            **_sess_kw,
        ) as session:
            # 1. Upload media (if any) and collect file_ids.
            file_ids: List[str] = []
            for media in media_files:
                file_path = media.get("path") if isinstance(media, dict) else media
                if not file_path or not os.path.exists(file_path):
                    continue
                form = aiohttp.FormData()
                # Mattermost requires channel_id on file uploads so the
                # server can attribute them.
                form.add_field("channel_id", chat_id)
                with open(file_path, "rb") as fh:
                    form.add_field(
                        "files",
                        fh.read(),
                        filename=os.path.basename(file_path),
                    )
                async with session.post(
                    f"{base_url}/api/v4/files",
                    data=form,
                    headers=upload_headers,
                    **_req_kw,
                ) as upload_resp:
                    if upload_resp.status not in {200, 201}:
                        body = await upload_resp.text()
                        return {
                            "error": (
                                f"Mattermost file upload failed "
                                f"({upload_resp.status}): {body[:400]}"
                            )
                        }
                    upload_data = await upload_resp.json()
                    for info in upload_data.get("file_infos", []):
                        if info.get("id"):
                            file_ids.append(info["id"])

            # 2. Post the message (with thread root + attached file_ids).
            payload: Dict[str, Any] = {
                "channel_id": chat_id,
                "message": message,
            }
            if thread_id:
                payload["root_id"] = thread_id
            if file_ids:
                payload["file_ids"] = file_ids
            async with session.post(
                f"{base_url}/api/v4/posts",
                headers=headers,
                json=payload,
                **_req_kw,
            ) as resp:
                if resp.status not in {200, 201}:
                    body = await resp.text()
                    return {
                        "error": (
                            f"Mattermost API error ({resp.status}): "
                            f"{body[:400]}"
                        )
                    }
                data = await resp.json()
            return {
                "success": True,
                "platform": "mattermost",
                "chat_id": chat_id,
                "message_id": data.get("id"),
            }
    except aiohttp.ClientError as exc:
        return {"error": f"Mattermost send failed (network): {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Mattermost send failed: {exc}"}


# ---------------------------------------------------------------------------
# Interactive setup wizard
# ---------------------------------------------------------------------------


def interactive_setup() -> None:
    """Guide the user through Mattermost bot setup.

    Mirrors Discord/Teams' ``interactive_setup`` shape: lazy-imports CLI
    helpers so the plugin's import surface stays small, prompts for the
    server URL + bot token, captures an allowlist, and offers to set a
    home channel.  Replaces the central
    ``hermes_cli/setup.py::_setup_mattermost`` function this migration
    removes.
    """
    from hermes_cli.config import get_env_value, remove_env_value, save_env_value
    from hermes_cli.cli_output import (
        prompt,
        prompt_yes_no,
        print_header,
        print_info,
        print_success,
    )

    print_header("Mattermost")
    existing = get_env_value("MATTERMOST_TOKEN")
    if existing:
        print_info("Mattermost: already configured")
        if not prompt_yes_no("Reconfigure Mattermost?", False):
            return

    print_info("Works with any self-hosted Mattermost instance.")
    print_info("   1. In Mattermost: Integrations → Bot Accounts → Add Bot Account")
    print_info("   2. Copy the bot token")
    print()
    mm_url = prompt("Mattermost server URL (e.g. https://mm.example.com)")
    if mm_url:
        save_env_value("MATTERMOST_URL", mm_url.rstrip("/"))
    token = prompt("Bot token", password=True)
    if not token:
        return
    save_env_value("MATTERMOST_TOKEN", token)
    print_success("Mattermost token saved")

    print()
    print_info("🔒 Security: Restrict who can use your bot")
    print_info("   To find your user ID: click your avatar → Profile")
    print_info("   or use the API: GET /api/v4/users/me")
    print()
    allowed_users = prompt("Allowed user IDs (comma-separated, leave empty for open access)")
    if allowed_users:
        save_env_value("MATTERMOST_ALLOWED_USERS", allowed_users.replace(" ", ""))
        print_success("Mattermost allowlist configured")
    else:
        print_info("⚠️  No allowlist set - anyone who can message the bot can use it!")

    print()
    print_info("📬 Home Channel: where Hermes delivers cron job results and notifications.")
    print_info("   To get a channel ID: click channel name → View Info → copy the ID")
    print_info("   You can also set this later by typing /set-home in a Mattermost channel.")
    home_channel = prompt("Home channel ID (leave empty to set later with /set-home)").strip()
    if home_channel:
        save_env_value("MATTERMOST_HOME_CHANNEL", home_channel)
    else:
        if remove_env_value("MATTERMOST_HOME_CHANNEL"):
            print_info("Home channel cleared.")
    print_info("   Open config in your editor:  hermes config edit")


# ---------------------------------------------------------------------------
# YAML → env config bridge (apply_yaml_config_fn, #25443)
# ---------------------------------------------------------------------------


def _apply_yaml_config(yaml_cfg: dict, mattermost_cfg: dict) -> dict | None:
    """Translate ``config.yaml`` ``mattermost:`` keys into env vars.

    Implements the ``apply_yaml_config_fn`` contract (#24836 / #25443).
    Mirrors the legacy ``mattermost_cfg`` block that used to live in
    ``gateway/config.py::load_gateway_config()`` before this migration.

    The MattermostAdapter reads its runtime configuration via
    ``os.getenv()`` for ``MATTERMOST_REQUIRE_MENTION``,
    ``MATTERMOST_FREE_RESPONSE_CHANNELS``, and
    ``MATTERMOST_ALLOWED_CHANNELS``.  Rather than rewrite those call sites
    to read from ``PlatformConfig.extra``, this hook keeps the env-driven
    model and merely owns the YAML→env translation here, next to the
    adapter that consumes it.

    Env vars take precedence over YAML — every assignment is guarded
    by ``not os.getenv(...)`` so an explicit env var survives a config.yaml
    update.  Returns ``None`` because no extras are seeded into
    ``PlatformConfig.extra`` directly (everything flows through env).
    """
    if "require_mention" in mattermost_cfg and not os.getenv("MATTERMOST_REQUIRE_MENTION"):
        os.environ["MATTERMOST_REQUIRE_MENTION"] = str(mattermost_cfg["require_mention"]).lower()
    frc = mattermost_cfg.get("free_response_channels")
    if frc is not None and not os.getenv("MATTERMOST_FREE_RESPONSE_CHANNELS"):
        if isinstance(frc, list):
            frc = ",".join(str(v) for v in frc)
        os.environ["MATTERMOST_FREE_RESPONSE_CHANNELS"] = str(frc)
    # allowed_channels: if set, bot ONLY responds in these channels (whitelist)
    ac = mattermost_cfg.get("allowed_channels")
    if ac is not None and not os.getenv("MATTERMOST_ALLOWED_CHANNELS"):
        if isinstance(ac, list):
            ac = ",".join(str(v) for v in ac)
        os.environ["MATTERMOST_ALLOWED_CHANNELS"] = str(ac)
    return None  # all settings flow through env; nothing to merge into extras


# ---------------------------------------------------------------------------
# is_connected probe
# ---------------------------------------------------------------------------


def _is_connected(config) -> bool:
    """Mattermost is considered connected when BOTH MATTERMOST_TOKEN and
    MATTERMOST_URL are set.

    Looks up via ``hermes_cli.gateway.get_env_value`` at call time (not via
    the plugin's own bound import) so tests that patch
    ``gateway_mod.get_env_value`` can suppress ambient env vars.  Matches
    what the legacy connected-platforms check did before this migration.
    """
    import hermes_cli.gateway as gateway_mod
    return bool(
        (gateway_mod.get_env_value("MATTERMOST_TOKEN") or "").strip()
        and (gateway_mod.get_env_value("MATTERMOST_URL") or "").strip()
    )


# ---------------------------------------------------------------------------
# Plugin registration entry point
# ---------------------------------------------------------------------------


def _build_adapter(config):
    """Factory wrapper that constructs MattermostAdapter from a PlatformConfig."""
    return MattermostAdapter(config)


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="mattermost",
        label="Mattermost",
        adapter_factory=_build_adapter,
        check_fn=check_mattermost_requirements,
        validate_config=validate_mattermost_config,
        is_connected=_is_connected,
        required_env=["MATTERMOST_URL", "MATTERMOST_TOKEN"],
        install_hint="pip install aiohttp",
        # Interactive setup wizard — replaces the central
        # hermes_cli/setup.py::_setup_mattermost function.
        setup_fn=interactive_setup,
        # YAML→env config bridge — owns the translation of
        # ``config.yaml`` ``mattermost:`` keys (require_mention,
        # free_response_channels, allowed_channels) into ``MATTERMOST_*``
        # env vars that the adapter reads via ``os.getenv()``.  Replaces
        # the hardcoded block that used to live in ``gateway/config.py``.
        # Hook contract: #24836 / #25443.
        apply_yaml_config_fn=_apply_yaml_config,
        # Auth env vars for _is_user_authorized() integration.
        allowed_users_env="MATTERMOST_ALLOWED_USERS",
        allow_all_env="MATTERMOST_ALLOW_ALL_USERS",
        # Cron home-channel delivery.
        cron_deliver_env_var="MATTERMOST_HOME_CHANNEL",
        # Out-of-process cron delivery via Mattermost REST API.  Without
        # this hook, ``deliver=mattermost`` cron jobs fail with "No live
        # adapter" when cron runs separately from the gateway.  Mirrors
        # the Discord / Teams pattern.
        standalone_sender_fn=_standalone_send,
        # Mattermost practical post-length limit (server default is 16383
        # but 4000 is the readable threshold the adapter has used since
        # day one).
        max_message_length=MAX_POST_LENGTH,
        # Display
        emoji="💬",
        allow_update_command=True,
    )
