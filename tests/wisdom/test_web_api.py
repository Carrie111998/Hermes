from __future__ import annotations

import asyncio

from hermes_cli import web_server
from hermes_cli.web_models import (
    WisdomInstallApplyRequest,
    WisdomSuggestRequest,
)


def test_suggest_bff_preserves_profile_and_owner_approved_fields(monkeypatch) -> None:
    calls: list[tuple[str | None, tuple[object, ...], dict[str, object]]] = []

    class Service:
        def suggest(self, *args, **kwargs):
            calls.append(("work", args, kwargs))
            return {"network_submission": True}

    async def run(profile, fn):
        calls.append((profile, (), {}))
        return fn(Service())

    monkeypatch.setattr(web_server, "_run_wisdom", run)
    body = WisdomSuggestRequest(
        skill="work",
        description="Outcome-oriented owner copy",
        system_specification={"auto_install": False},
        send_for_owner_only_server_review=True,
        profile="customer-a",
    )

    result = asyncio.run(web_server.post_wisdom_suggest(body))

    assert result == {"network_submission": True}
    assert calls == [
        ("customer-a", (), {}),
        (
            "work",
            ("work",),
            {
                "description": "Outcome-oriented owner copy",
                "system_specification": {"auto_install": False},
                "allow_private_secret_review": True,
            },
        ),
    ]


def test_install_apply_bff_requires_a_plan_receipt(monkeypatch) -> None:
    calls: list[tuple[str | None, str, bool]] = []

    class Service:
        def install_apply(self, receipt: str, *, accept_partial: bool):
            calls.append((None, receipt, accept_partial))
            return {"state": "installed"}

    async def run(profile, fn):
        result = fn(Service())
        calls[0] = (profile, calls[0][1], calls[0][2])
        return result

    monkeypatch.setattr(web_server, "_run_wisdom", run)
    body = WisdomInstallApplyRequest(
        receipt="receipt-123", accept_partial=True, profile="customer-b"
    )

    result = asyncio.run(web_server.post_wisdom_install_apply(body))

    assert result == {"state": "installed"}
    assert calls == [("customer-b", "receipt-123", True)]


def test_wisdom_error_mapping_is_opaque_and_bounded() -> None:
    from hermes_wisdom.client import WisdomError, WisdomNotFound

    assert web_server._wisdom_http_error(WisdomNotFound("not found")).status_code == 404
    assert web_server._wisdom_http_error(WisdomError("retry")).status_code == 503
