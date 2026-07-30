import io
import json
import os
import stat
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

import grover_runtime.action_service_client as action_service_client
from grover_runtime.action_service_client import (
    ActionServiceClient,
    ActionServiceError,
    render_shadow_card,
)


class RecordingOpener:
    def __init__(self, payload):
        self.payload = payload
        self.request = None
        self.timeout = None

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        return SimpleNamespace(
            read=lambda _limit: json.dumps(self.payload).encode(),
            geturl=lambda: request.full_url,
        )


class RawResponseOpener:
    def __init__(self, body: bytes):
        self.body = body

    def open(self, request, timeout):
        return SimpleNamespace(
            read=lambda _limit: self.body,
            geturl=lambda: request.full_url,
        )


def _token(tmp_path):
    path = tmp_path / "bridge-token"
    path.write_text("T" * 48, encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def test_resolve_uses_owner_token_and_typed_message_context(tmp_path):
    payload = {
        "card_ref": "TGC-0123456789abcdef",
        "receipt_id": "ACT-0123456789ab",
        "action": "approve",
        "mode": "shadow",
    }
    opener = RecordingOpener(payload)
    client = ActionServiceClient(token_path=_token(tmp_path), opener=opener)

    assert (
        client.resolve_callback(
            "od:" + "A" * 24, "-1004474237403", "321", "123456", "Kevin"
        )
        == payload
    )

    assert opener.request.full_url == "http://127.0.0.1:8791/api/v1/telegram/resolve"
    assert opener.request.get_header("X-action-bridge-token") == "T" * 48
    assert json.loads(opener.request.data) == {
        "callback": "od:" + "A" * 24,
        "chat_id": "-1004474237403",
        "message_id": "321",
        "telegram_user_id": "123456",
        "actor_label": "Kevin",
    }


def test_world_readable_bridge_token_is_rejected_before_network(tmp_path):
    if os.name == "nt":
        pytest.skip("Windows does not expose POSIX owner-only mode bits")
    token_path = _token(tmp_path)
    token_path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)
    opener = RecordingOpener({})
    client = ActionServiceClient(token_path=token_path, opener=opener)

    with pytest.raises(ActionServiceError, match="owner-only"):
        client.pending()

    assert opener.request is None


def test_secure_token_loader_reads_from_one_descriptor_without_path_toctou(
    tmp_path, monkeypatch
):
    token_path = _token(tmp_path)
    original = "T" * 48
    replacement = "A" * 48
    real_stat = Path.stat
    swapped = False

    def swapping_stat(path, *args, **kwargs):
        nonlocal swapped
        result = real_stat(path, *args, **kwargs)
        if path == token_path and not swapped:
            swapped = True
            token_path.unlink()
            token_path.write_text(replacement, encoding="utf-8")
        return result

    monkeypatch.setattr(Path, "stat", swapping_stat)

    assert ActionServiceClient(token_path=token_path)._read_owner_token() == original
    assert swapped is False, "secure descriptor reads must not stat then reopen by path"


def test_secure_token_loader_validates_the_open_descriptor_not_a_path_snapshot(
    tmp_path, monkeypatch
):
    token_path = _token(tmp_path)
    real_fstat = action_service_client.os.fstat

    def insecure_fstat(fd):
        current = real_fstat(fd)
        return SimpleNamespace(
            st_uid=getattr(current, "st_uid", 0) + 1,
            st_mode=stat.S_IFREG | 0o644,
            st_size=current.st_size,
        )

    monkeypatch.setattr(action_service_client.os, "name", "posix")
    monkeypatch.setattr(action_service_client.os, "fstat", insecure_fstat)

    with pytest.raises(ActionServiceError, match="owner mismatch"):
        ActionServiceClient(token_path=token_path)._read_owner_token()


def test_http_error_body_is_not_exposed(tmp_path):
    class FailingOpener:
        def open(self, _request, timeout):
            raise urllib.error.HTTPError(
                "http://127.0.0.1:8791/api/v1/telegram/pending",
                500,
                "failed",
                {},
                io.BytesIO(b"private service detail"),
            )

    client = ActionServiceClient(token_path=_token(tmp_path), opener=FailingOpener())

    with pytest.raises(ActionServiceError) as caught:
        client.pending()

    assert "HTTP 500" in str(caught.value)
    assert "private service detail" not in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        b'{"mode":"prod","mode":"shadow","items":[]}',
        b'{"mode":"shadow","items":[],"score":NaN}',
        b'{"mode":"shadow","items":[],"score":Infinity}',
    ],
)
def test_ambiguous_or_non_finite_service_json_is_rejected(tmp_path, body):
    client = ActionServiceClient(
        token_path=_token(tmp_path), opener=RawResponseOpener(body)
    )

    with pytest.raises(ActionServiceError, match="response is invalid"):
        client.pending()


@pytest.mark.parametrize(
    "response_url",
    [
        "http://attacker.example/api/v1/telegram/pending",
        "http://127.0.0.1:8791/api/v1/telegram/mirrored",
        "http://localhost:8791/api/v1/telegram/pending",
    ],
)
def test_redirected_or_non_exact_loopback_response_is_rejected(tmp_path, response_url):
    class RedirectingOpener:
        def open(self, request, timeout):
            return SimpleNamespace(
                read=lambda _limit: b'{"mode":"shadow","items":[]}',
                geturl=lambda: response_url,
            )

    client = ActionServiceClient(
        token_path=_token(tmp_path), opener=RedirectingOpener()
    )

    with pytest.raises(ActionServiceError, match="unexpected endpoint"):
        client.pending()


def test_default_transport_ignores_environment_proxies_and_redirects(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HTTP_PROXY", "http://attacker.example:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.example:8080")

    client = ActionServiceClient(token_path=_token(tmp_path))

    proxy_handlers = [
        handler
        for handler in client.opener.handlers
        if isinstance(handler, urllib.request.ProxyHandler)
    ]
    # build_opener intentionally omits an empty ProxyHandler from its final
    # chain. Its absence proves urllib did not install the environment-backed
    # default handler.
    assert proxy_handlers == []
    assert any(
        handler.__class__.__name__ == "_NoRedirect"
        for handler in client.opener.handlers
    )


def _pending_item():
    return {
        "card_ref": "TGC-0123456789abcdef",
        "binding": {
            "chat_id": "-1004474237403",
            "thread_id": "91",
            "message_id": "321",
            "card_html": "<b>Decision</b>",
        },
        "resolution": {
            "receipt_id": "ACT-0123456789ab",
            "action": "approve",
            "actor_label": "Kevin",
            "surface": "telegram",
            "mode": "shadow",
        },
    }


def test_pending_card_is_bound_to_exact_route_and_receipt(tmp_path):
    pending = _pending_item()
    client = ActionServiceClient(
        token_path=_token(tmp_path),
        opener=RecordingOpener({"mode": "shadow", "items": [pending]}),
    )

    assert (
        client.pending_card(
            "TGC-0123456789abcdef",
            chat_id="-1004474237403",
            thread_id="91",
            message_id="321",
            receipt_id="ACT-0123456789ab",
            action="approve",
        )
        == pending
    )


def test_pending_card_accepts_a_bound_non_topic_message(tmp_path):
    pending = _pending_item()
    pending["binding"]["thread_id"] = None
    client = ActionServiceClient(
        token_path=_token(tmp_path),
        opener=RecordingOpener({"mode": "shadow", "items": [pending]}),
    )

    assert (
        client.pending_card(
            "TGC-0123456789abcdef",
            chat_id="-1004474237403",
            thread_id=None,
            message_id="321",
            receipt_id="ACT-0123456789ab",
            action="approve",
        )
        == pending
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("chat_id", "-1009999999999"),
        ("thread_id", "92"),
        ("message_id", "322"),
        ("receipt_id", "ACT-ffffffffffff"),
        ("action", "reject"),
    ],
)
def test_pending_card_rejects_tampered_route_or_receipt(tmp_path, field, value):
    pending = _pending_item()
    client = ActionServiceClient(
        token_path=_token(tmp_path),
        opener=RecordingOpener({"mode": "shadow", "items": [pending]}),
    )
    expected = {
        "chat_id": "-1004474237403",
        "thread_id": "91",
        "message_id": "321",
        "receipt_id": "ACT-0123456789ab",
        "action": "approve",
    }
    expected[field] = value

    with pytest.raises(ActionServiceError, match="does not match"):
        client.pending_card("TGC-0123456789abcdef", **expected)


def test_receipt_renderer_escapes_actor_and_preserves_shadow_honesty():
    item = {
        "card_ref": "TGC-0123456789abcdef",
        "binding": {
            "chat_id": "-1004474237403",
            "thread_id": "91",
            "message_id": "321",
            "card_html": "<b>Decision</b>",
        },
        "resolution": {
            "receipt_id": "ACT-0123456789ab",
            "action": "reject",
            "actor_label": "<Kevin>",
            "mode": "shadow",
        },
    }

    rendered = render_shadow_card(item)

    assert "&lt;Kevin&gt;" in rendered
    assert "<Kevin>" not in rendered
    assert "Recorded only — nothing was executed." in rendered
    assert "ACT-0123456789ab" in rendered
