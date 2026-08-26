"""Isolated fake peer used by the two-host RoomLink UAT recipe."""

import os
import time

from aiohttp import web

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


class FakeAgent:
    session_prompt_tokens = 0
    session_completion_tokens = 0
    session_total_tokens = 0

    def run_conversation(self, *_args, **_kwargs):
        time.sleep(float(os.getenv("UAT_REPLY_DELAY", "2")))
        return {"final_response": "REMOTE_UAT_REPLY"}


def app():
    key = os.environ["API_SERVER_KEY"]
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": key}))
    adapter._create_agent = lambda *_args, **_kwargs: FakeAgent()
    result = web.Application()
    result.router.add_post(
        "/v1/room-members/invitations",
        adapter._handle_room_member_invitation,
    )
    result.router.add_get(
        "/v1/room-members/capabilities",
        adapter._handle_room_member_capabilities,
    )
    result.router.add_post(
        "/v1/room-members/grants/refresh",
        adapter._handle_room_member_grant_refresh,
    )
    result.router.add_post(
        "/v1/room-members/grants/revoke",
        adapter._handle_room_member_grant_revoke,
    )
    result.router.add_post("/v1/runs", adapter._handle_runs)
    result.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    result.router.add_post("/v1/runs/{run_id}/stop", adapter._handle_stop_run)
    return result


web.run_app(
    app(),
    host=os.getenv("UAT_HOST", "127.0.0.1"),
    port=int(os.getenv("UAT_PORT", "8000")),
)
