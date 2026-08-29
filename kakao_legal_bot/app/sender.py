"""Getting text back into the KakaoTalk room.

Split long answers, then deliver by whichever route the deployment has:
straight to Iris, into the outbox for the relay to pull, or direct with an
outbox fallback. A reply that cannot be delivered right now is queued, not
dropped — the client is waiting on the other end.
"""

from __future__ import annotations

import asyncio
import logging

from .config import Settings
from .db import Database
from .iris import IrisClient, IrisSendError, split_for_kakao

log = logging.getLogger(__name__)


class Sender:
    def __init__(self, settings: Settings, db: Database, iris: IrisClient) -> None:
        self._settings = settings
        self._db = db
        self._iris = iris

    async def send(self, room_id: str, text: str, *, record_role: str = "bot") -> bool:
        """Deliver ``text`` to ``room_id``. Returns True if Iris took it."""
        chunks = split_for_kakao(text, self._settings.kakao_max_chars)
        if not chunks:
            return False

        delivered = True
        for index, chunk in enumerate(chunks):
            if index:
                # Kakao reorders messages that arrive in the same instant.
                await asyncio.sleep(0.4)
            delivered = await self._deliver(room_id, chunk) and delivered

        if record_role:
            await asyncio.to_thread(
                self._db.add_message,
                room_id,
                record_role,
                text,
                self._settings.bot_name,
                "",
                self._settings.history_turns,
            )
        return delivered

    async def _deliver(self, room_id: str, chunk: str) -> bool:
        mode = self._settings.iris_send_mode
        if mode == "poll":
            await asyncio.to_thread(self._db.enqueue_outbox, room_id, chunk)
            return False
        try:
            await self._iris.send_text(room_id, chunk)
            return True
        except IrisSendError as exc:
            if mode == "hybrid":
                log.warning("iris direct send failed (%s) — queueing to outbox", exc)
                await asyncio.to_thread(self._db.enqueue_outbox, room_id, chunk)
                return False
            log.error("iris send failed: %s", exc)
            raise

    async def notify_lawyer(self, text: str) -> bool:
        """Ping the lawyer's own room. No-op when it is not configured.

        방은 두 곳에서 옵니다 — 환경변수 ``LAWYER_ROOM_ID``, 또는 변호사가
        자기 1:1 방에서 ``/등록 <토큰>`` 을 보내 kv 에 저장한 방. 후자 덕에
        방 id 를 몰라도 등록 한 번이면 알림이 붙습니다.
        """
        room = self._settings.lawyer_room_id
        if not room:
            room = await asyncio.to_thread(self._db.kv_get, "lawyer_room_id")
        if not room:
            log.info("변호사 방 미등록 — 알림 생략: %s", text[:80])
            return False
        return await self.send(room, text, record_role="")
