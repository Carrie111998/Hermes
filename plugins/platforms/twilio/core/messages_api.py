"""Shared transport for Twilio's Messages.json resource. Channels build a
list of form-field dicts (one per API call) and pass them to
send_message_requests. Not for Voice (Calls.json) or Email
(comms.twilio.com's JSON REST API, an entirely different request/response
shape) — those need their own transport, built directly into their own
channel module (see channels/email.py)."""

import logging
from typing import Any, Dict, List, Optional

from gateway.platforms.helpers import redact_phone

from .credentials import TWILIO_API_BASE, basic_auth_header

logger = logging.getLogger(__name__)


def aiohttp_available() -> bool:
    try:
        import aiohttp  # noqa: F401

        return True
    except ImportError:
        return False


async def send_message_requests(
    account_sid: str,
    auth_token: str,
    form_fields_list: List[Dict[str, str]],
    chat_id: str,
    *,
    session=None,
    session_kwargs: Optional[dict] = None,
    request_kwargs: Optional[dict] = None,
    log_prefix: str = "[twilio]",
) -> Dict[str, Any]:
    """POST each form-field dict to Messages.json in order; stops on first
    failure, else returns the last result. session_kwargs/request_kwargs
    are for standalone callers passing proxy settings.

    Returns {"success": True, "message_id": sid} or {"success": False, "error": ...}.
    """
    import aiohttp

    url = f"{TWILIO_API_BASE}/{account_sid}/Messages.json"
    headers = {"Authorization": basic_auth_header(account_sid, auth_token)}
    session_kwargs = session_kwargs or {}
    request_kwargs = request_kwargs or {}

    owns_session = session is None
    session = session or aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30), trust_env=True, **session_kwargs
    )
    last_result: Dict[str, Any] = {"success": True}
    try:
        for form_fields in form_fields_list:
            form_data = aiohttp.FormData()
            for key, value in form_fields.items():
                form_data.add_field(key, value)
            try:
                async with session.post(
                    url, data=form_data, headers=headers, **request_kwargs
                ) as resp:
                    body = await resp.json()
                    if resp.status >= 400:
                        error_msg = body.get("message", str(body))
                        logger.error(
                            "%s send failed to %s: %s %s",
                            log_prefix, redact_phone(chat_id), resp.status, error_msg,
                        )
                        return {
                            "success": False,
                            "error": f"Twilio {resp.status}: {error_msg}",
                        }
                    last_result = {"success": True, "message_id": body.get("sid", "")}
            except Exception as e:
                logger.error("%s send error to %s: %s", log_prefix, redact_phone(chat_id), e)
                return {"success": False, "error": str(e)}
    finally:
        if owns_session:
            await session.close()
    return last_result
