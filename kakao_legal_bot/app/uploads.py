"""증거 사진·파일 접수 — 카카오톡이 못 주는 것을 카톡 밖에서 받는다.

폰 브리지(알림 방식)는 사진·파일의 내용을 받을 수 없습니다 — 알림에는
"사진을 보냈습니다"라는 글자만 실리니까요. 그래서 방마다 고유한 업로드
링크(``/u/<토큰>``)를 만들어 상담자 폰에서 우리 서버로 직접 올리게 합니다.

올라온 파일은 즉시:
- ``DATA_DIR/uploads/<방>/`` 에 원본 저장 (uploads 표에 목록)
- 변호사 카톡으로 📎 알림 + 실시간 방 화면에서 바로 열람
- 대화 기록에 시스템 메시지로 남아 **AI 도 자료가 왔다는 사실을 알고**
  다음 답변에서 언급할 수 있습니다.

토큰은 방마다 하나, kv 에 저장되어 링크가 항상 같습니다. 추측이 어렵게
무작위(token_urlsafe)로 만들고, 토큰으로는 업로드만 할 수 있습니다 —
열람은 변호사 화면(ADMIN_TOKEN)에서만.
"""

from __future__ import annotations

import re
import secrets
import time
import uuid
from pathlib import Path

from .config import Settings
from .db import Database

# 카카오톡 알림이 미디어를 대신해 보내는 문구들. 이 말이 오면 내용 대신
# 업로드 안내를 보냅니다.
_MEDIA_NOTICES: tuple[tuple[str, str], ...] = (
    ("사진을 보냈습니다", "사진"),
    ("사진 여러 장을 보냈습니다", "사진"),
    ("동영상을 보냈습니다", "동영상"),
    ("파일을 보냈습니다", "파일"),
    ("음성메시지를 보냈습니다", "음성"),
    ("음성 메시지를 보냈습니다", "음성"),
)
_MEDIA_EXACT = {"사진": "사진", "동영상": "동영상", "파일": "파일", "음성메시지": "음성"}


def media_kind(text: str) -> str | None:
    """알림이 미디어 자리표시 문구면 그 종류를, 아니면 None."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return None
    if cleaned in _MEDIA_EXACT:
        return _MEDIA_EXACT[cleaned]
    for needle, kind in _MEDIA_NOTICES:
        if cleaned == needle or cleaned.endswith(": " + needle):
            return kind
    return None


# ── 방별 업로드 토큰 ─────────────────────────────────────────────────────
def upload_token_for(db: Database, room_id: str) -> str:
    """이 방의 업로드 토큰 — 없으면 만들고, 있으면 그대로 (링크 불변)."""
    token = db.kv_get(f"upload_token:{room_id}")
    if token:
        return token
    token = secrets.token_urlsafe(9)
    db.kv_set(f"upload_token:{room_id}", token)
    db.kv_set(f"upload_room:{token}", room_id)
    return token


def room_for_upload_token(db: Database, token: str) -> str:
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", token):
        return ""
    return db.kv_get(f"upload_room:{token}")


def upload_url(settings: Settings, db: Database, room_id: str) -> str:
    """상담자에게 보낼 업로드 링크. PUBLIC_BASE_URL 이 없으면 빈 문자열."""
    if not settings.public_base_url:
        return ""
    return f"{settings.public_base_url}/u/{upload_token_for(db, room_id)}"


def upload_guidance(settings: Settings, db: Database, room_id: str, kind: str) -> str:
    """"사진을 보냈습니다"에 대신 나가는 안내문."""
    link = upload_url(settings, db, room_id)
    if kind == "음성":
        voice = (
            "음성 메시지는 이 방에서 재생해 드릴 수 없습니다 🙏\n"
            "키보드의 마이크(음성 입력)로 말씀하시면 글자로 바뀌어 전달되고, "
            "녹음 파일 자체가 증거라면 아래 링크로 올려주세요.\n"
        )
    else:
        voice = ""
    if not link:
        return (
            f"{voice}보내주신 {kind}은(는) 이 채팅방에서 제가 열람할 수 없습니다 🙏\n"
            "/이메일 로 주소를 등록하신 뒤 그 주소로 자료를 보내주시면 "
            "변호사님이 직접 확인합니다."
        )
    return (
        f"{voice}보내주신 {kind}은(는) 이 채팅방에서 제가 직접 열람할 수 없어, "
        "전용 자료 접수 페이지를 안내드립니다.\n\n"
        f"📎 자료 올리기: {link}\n\n"
        "위 링크는 상담자님 전용입니다. 올려주시는 즉시 변호사님과 저에게 "
        "전달되어 상담에 반영됩니다."
    )


# ── 저장 ─────────────────────────────────────────────────────────────────
_SAFE_ROOM = re.compile(r"[^A-Za-z0-9._-]")


def _room_folder(settings: Settings, room_id: str) -> Path:
    safe = _SAFE_ROOM.sub("_", room_id)[:64] or "room"
    return settings.upload_dir / safe


def clean_filename(name: str) -> str:
    """경로 조작을 막고 읽을 수 있는 이름만 남긴다."""
    base = Path(name or "").name  # 디렉터리 부분 제거
    base = base.replace("\x00", "").strip().strip(".")
    return base[:120] or "파일"


def save_upload(
    settings: Settings,
    db: Database,
    room_id: str,
    filename: str,
    content: bytes,
    content_type: str = "",
) -> tuple[int, Path]:
    """파일 하나 저장 + 기록. 반환: (upload_id, 저장 경로)."""
    name = clean_filename(filename)
    suffix = Path(name).suffix.lower()[:10]
    stored = f"{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:10]}{suffix}"
    folder = _room_folder(settings, room_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / stored
    path.write_bytes(content)
    upload_id = db.add_upload(room_id, name, stored, content_type, len(content))
    return upload_id, path


def stored_path(settings: Settings, row) -> Path:  # noqa: ANN001 — sqlite3.Row
    return _room_folder(settings, str(row["room_id"])) / str(row["stored_name"])


def human_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f}MB"
    if size >= 1024:
        return f"{size / 1024:.0f}KB"
    return f"{size}B"
