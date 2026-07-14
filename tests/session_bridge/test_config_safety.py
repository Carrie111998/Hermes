from __future__ import annotations

import re
from pathlib import Path

import pytest

from session_bridge.config import BridgeConfig


def _load(
    path: Path,
    *,
    environ: dict[str, str] | None = None,
) -> BridgeConfig:
    return BridgeConfig.load(path=path, environ={} if environ is None else environ)


@pytest.mark.parametrize(
    "host",
    (
        "127.0.0.2",
        "127.1.2.3",
        "127.255.255.254",
        "0:0:0:0:0:0:0:1",
        "0000:0000:0000:0000:0000:0000:0000:0001",
    ),
)
@pytest.mark.parametrize("from_environment", (False, True))
def test_canonical_loopback_variants_are_accepted_without_a_toml_grant(
    tmp_path: Path,
    host: str,
    from_environment: bool,
) -> None:
    path = tmp_path / "session_bridge.toml"
    if from_environment:
        environ = {"HERMES_SESSION_BRIDGE_HOST": host}
    else:
        path.write_text(f'[service]\nhost = "{host}"\n', encoding="utf-8")
        environ = {}

    config = _load(path, environ=environ)

    assert config.service.host == host.lower()
    assert config.service.allow_non_loopback is False


@pytest.mark.parametrize(
    "host",
    (
        "0.0.0.0",
        "126.255.255.255",
        "128.0.0.0",
        "192.0.2.10",
        "::",
        "2001:db8::1",
        "127.0.0.1.example.com",
    ),
)
@pytest.mark.parametrize("from_environment", (False, True))
def test_non_loopback_hosts_still_require_an_explicit_toml_grant(
    tmp_path: Path,
    host: str,
    from_environment: bool,
) -> None:
    path = tmp_path / "session_bridge.toml"
    if from_environment:
        environ = {"HERMES_SESSION_BRIDGE_HOST": host}
    else:
        path.write_text(f'[service]\nhost = "{host}"\n', encoding="utf-8")
        environ = {}

    with pytest.raises(ValueError, match="non-loopback"):
        _load(path, environ=environ)


@pytest.mark.parametrize(
    "name",
    (
        "HERMES_SESSION_BRIDGE_CATALGO_ENABLED",
        "HERMES_SESSION_BRIDGE_SERVICE_PORT",
        "HERMES_SESSION_BRIDGE_UNSUPPORTED",
    ),
)
def test_unknown_bridge_environment_variables_are_rejected(
    tmp_path: Path,
    name: str,
) -> None:
    with pytest.raises(ValueError, match=re.escape(name)):
        _load(tmp_path / "missing.toml", environ={name: "true"})


def test_environment_cannot_grant_non_loopback_access(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="explicitly in TOML"):
        _load(
            tmp_path / "missing.toml",
            environ={
                "HERMES_SESSION_BRIDGE_HOST": "192.0.2.10",
                "HERMES_SESSION_BRIDGE_ALLOW_NON_LOOPBACK": "true",
            },
        )


def test_mcp_token_is_whitelisted_but_not_persisted_in_config(tmp_path: Path) -> None:
    config = _load(
        tmp_path / "missing.toml",
        environ={"HERMES_SESSION_BRIDGE_TOKEN": "x" * 32},
    )

    assert config == BridgeConfig()
    assert not hasattr(config, "token")
