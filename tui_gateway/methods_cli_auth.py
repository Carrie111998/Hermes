"""Gateway-scoped Claude Code and Codex CLI authentication RPCs."""
from __future__ import annotations

from .method_ctx import HandlerRegistry

_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped


def register(ctx: dict) -> None:
    _registry.install(ctx)


@method("auth.cli.accounts")
@_profile_scoped
def _(rid, params: dict) -> dict:
    try:
        from hermes_cli.cli_auth_broker import CliAuthBroker

        provider = str(params.get("provider") or "").strip().lower()
        if not provider:
            return _err(rid, 4003, "provider required")
        return _ok(
            rid,
            {
                "provider": provider,
                "accounts": CliAuthBroker().list_statuses(provider),
            },
        )
    except ValueError as exc:
        return _err(rid, 4003, str(exc))
    except Exception:
        return _err(rid, 5029, "CLI account status unavailable")


@method("auth.cli.start")
@_profile_scoped
def _(rid, params: dict) -> dict:
    try:
        from hermes_cli.cli_auth_broker import CliAuthBroker

        provider = str(params.get("provider") or "").strip().lower()
        account_id = str(params.get("account_id") or "").strip().lower()
        if not provider or not account_id:
            return _err(rid, 4003, "provider and account_id required")
        return _ok(rid, CliAuthBroker().start(provider, account_id))
    except ValueError as exc:
        return _err(rid, 4003, str(exc))
    except Exception:
        return _err(rid, 5029, "CLI authorization could not be started")


@method("auth.cli.poll")
@_profile_scoped
def _(rid, params: dict) -> dict:
    try:
        from hermes_cli.cli_auth_broker import CliAuthBroker

        provider = str(params.get("provider") or "").strip().lower()
        account_id = str(params.get("account_id") or "").strip().lower()
        session_id = str(params.get("session_id") or "").strip()
        if not provider or not account_id or not session_id:
            return _err(rid, 4003, "provider, account_id and session_id required")
        return _ok(rid, CliAuthBroker().poll(provider, account_id, session_id))
    except ValueError as exc:
        return _err(rid, 4003, str(exc))
    except Exception:
        return _err(rid, 5029, "CLI authorization status unavailable")


@method("auth.cli.submit")
@_profile_scoped
def _(rid, params: dict) -> dict:
    try:
        from hermes_cli.cli_auth_broker import CliAuthBroker

        provider = str(params.get("provider") or "").strip().lower()
        account_id = str(params.get("account_id") or "").strip().lower()
        session_id = str(params.get("session_id") or "").strip()
        code = str(params.get("code") or "")
        if not provider or not account_id or not session_id:
            return _err(rid, 4003, "provider, account_id and session_id required")
        if not code.strip():
            return _err(rid, 4003, "authorization code required")
        return _ok(rid, CliAuthBroker().submit(provider, account_id, session_id, code))
    except ValueError as exc:
        return _err(rid, 4003, str(exc))
    except Exception:
        return _err(rid, 5029, "CLI authorization code could not be submitted")


@method("auth.cli.cancel")
@_profile_scoped
def _(rid, params: dict) -> dict:
    try:
        from hermes_cli.cli_auth_broker import CliAuthBroker

        provider = str(params.get("provider") or "").strip().lower()
        account_id = str(params.get("account_id") or "").strip().lower()
        session_id = str(params.get("session_id") or "").strip()
        if not provider or not account_id or not session_id:
            return _err(rid, 4003, "provider, account_id and session_id required")
        return _ok(rid, CliAuthBroker().cancel(provider, account_id, session_id))
    except ValueError as exc:
        return _err(rid, 4003, str(exc))
    except Exception:
        return _err(rid, 5029, "CLI authorization could not be cancelled")
