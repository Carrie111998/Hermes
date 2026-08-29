"""올라온 증거 자료를 제미나이가 즉시 읽는다 — 사진·PDF·녹음.

업로드 페이지로 파일이 들어오면 백그라운드에서 이 모듈이 제미나이 비전에
파일을 통째로 보내 요약을 받습니다. 요약은 세 곳으로 갑니다:

- ``uploads.summary`` — 실시간 방 화면에서 파일 옆에 표시
- 대화 기록의 시스템 메시지 — **다음 턴부터 AI 가 자료 내용을 알고 답변**
- 상담방·변호사 카톡 — "확인했습니다" 한 통

분석은 참고용입니다. 판독이 안 되면 안 됐다고 말하게 프롬프트로 눌러
두었고, 원본 판단은 언제나 변호사 몫입니다.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger(__name__)

# 제미나이에 인라인으로 보낼 수 있는 종류. 그 밖(한글 hwp, zip 등)은
# 변호사가 직접 여는 수밖에 없습니다.
_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif",
    ".heic": "image/heic", ".heif": "image/heif",
    ".pdf": "application/pdf",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".ogg": "audio/ogg", ".wav": "audio/wav", ".amr": "audio/amr",
}
ANALYZABLE_MIMES = frozenset(_MIME_BY_SUFFIX.values())

# base64 로 1/3 부풀고 요청 한도가 있으니, 이보다 크면 분석은 건너뛰고
# 원본 보관·변호사 열람만 합니다.
MAX_ANALYZE_BYTES = 10 * 1024 * 1024

_PROMPT = """당신은 변호사 사무실의 증거자료 검토 보조자입니다. 첨부 파일을 읽고
법률상담에 필요한 사실만 간결히 정리하세요 (5줄 이내):
- 문서/사진의 종류 (계약서, 차용증, 문자 캡처, 진단서, 영수증 등)
- 등장하는 사람·회사 이름과 역할
- 날짜, 금액, 기한 등 숫자
- 핵심 내용 한두 문장
- 음성 파일이면 대화 내용을 요약 (누가 무슨 말을 했는지)
읽을 수 없거나 불명확한 부분은 추측하지 말고 "판독 불가"라고 적으세요."""


def mime_for(filename: str, content_type: str = "") -> str:
    """분석 가능한 MIME 이면 그 값을, 아니면 빈 문자열."""
    declared = (content_type or "").split(";")[0].strip().lower()
    if declared in ANALYZABLE_MIMES:
        return declared
    return _MIME_BY_SUFFIX.get(Path(filename or "").suffix.lower(), "")


def build_request(model: str, mime: str, data: bytes, max_tokens: int = 700) -> dict[str, Any]:
    """generateContent 요청 본문 — 파일 인라인 + 분석 지시. (순수 함수, 테스트용)"""
    return {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}},
                    {"text": _PROMPT},
                ],
            }
        ],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": max_tokens},
    }


def _gemini_model(settings: Settings) -> str:
    if settings.llm_provider == "gemini" and settings.llm_model:
        return settings.llm_model
    return "gemini-3.7-flash"


async def describe_file(
    settings: Settings, path: Path, filename: str, content_type: str = ""
) -> str:
    """파일 하나를 제미나이로 분석. 못 하면(키 없음·형식·크기) 빈 문자열.

    실패가 업로드 흐름을 깨면 안 되므로 예외는 전부 삼키고 로그만 남깁니다.
    """
    if not settings.gemini_api_key:
        return ""
    mime = mime_for(filename, content_type)
    if not mime:
        return ""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if not data or len(data) > MAX_ANALYZE_BYTES:
        return ""

    model = _gemini_model(settings)
    url = f"{settings.gemini_base_url}/models/{model}:generateContent"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                url,
                headers={
                    "x-goog-api-key": settings.gemini_api_key,
                    "content-type": "application/json",
                },
                json=build_request(model, mime, data),
            )
        if response.status_code >= 400:
            log.warning("vision %s: %s %s", filename, response.status_code, response.text[:200])
            return ""
        payload = response.json()
        candidates = payload.get("candidates") or []
        if not candidates:
            return ""
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "\n".join(
            part["text"] for part in parts if isinstance(part.get("text"), str)
        ).strip()
        return text
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("vision %s failed: %s", filename, exc)
        return ""
