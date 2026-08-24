from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from hermes_cli import web_server
from hermes_cli.web_models import (
    WisdomInstallApplyRequest,
    WisdomSetupRequest,
    WisdomSuggestRequest,
    WisdomUpdateApplyRequest,
)


def test_setup_bff_forwards_explicit_disclosure_with_profile_scope(monkeypatch):
    calls = []
    monkeypatch.setattr(
        web_server, "_profile_cli_args", lambda profile: ["-p", str(profile)]
    )

    def spawn(command, name):
        calls.append((command, name))
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(web_server, "_spawn_hermes_action", spawn)
    result = asyncio.run(
        web_server.post_wisdom_setup(
            WisdomSetupRequest(accept_disclosure=True, profile="research")
        )
    )
    assert result["ok"] is True
    assert result["pid"] == 123
    assert calls == [
        (
            [
                "-p",
                "research",
                "wisdom",
                "setup",
                "--accept-disclosure",
                "--json",
            ],
            result["name"],
        )
    ]

    with pytest.raises(HTTPException) as rejected:
        asyncio.run(
            web_server.post_wisdom_setup(
                WisdomSetupRequest(accept_disclosure=False, profile="research")
            )
        )
    assert rejected.value.status_code == 422


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


def test_update_bff_forwards_only_explicit_confirmation_flags(monkeypatch) -> None:
    calls = []

    class Service:
        def update_apply(self, receipt, **kwargs):
            calls.append((receipt, kwargs))
            return {"updated": True}

    async def run(profile, fn):
        calls.append(profile)
        return fn(Service())

    monkeypatch.setattr(web_server, "_run_wisdom", run)
    body = WisdomUpdateApplyRequest(
        receipt="wup_123",
        accept_sensitive=True,
        accept_partial=False,
        preserve_modified=True,
        profile="research",
    )
    result = asyncio.run(web_server.post_wisdom_update_apply(body))
    assert result == {"updated": True}
    assert calls == [
        "research",
        (
            "wup_123",
            {
                "accept_sensitive": True,
                "accept_partial": False,
                "preserve_modified": True,
            },
        ),
    ]
