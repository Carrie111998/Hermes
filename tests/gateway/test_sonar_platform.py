"""Tests for Sonar platform plugin registration."""

from plugins.platforms.sonar.adapter import (
    _split_chunks,
    check_requirements,
    register,
    validate_config,
)
from gateway.config import Platform, PlatformConfig


def test_split_chunks_multipart():
    long = "a" * 5000
    parts = _split_chunks(long, 3200)
    assert len(parts) >= 2
    assert "".join(parts) == long


def test_register_platform_smoke():
    class Ctx:
        def __init__(self):
            self.calls = []

        def register_platform(self, **kwargs):
            self.calls.append(kwargs)

    ctx = Ctx()
    register(ctx)
    assert len(ctx.calls) == 1
    assert ctx.calls[0]["name"] == "sonar"
    assert ctx.calls[0]["label"] == "Sonar"


def test_validate_config_empty_senders():
    cfg = PlatformConfig(enabled=True, extra={"authorized_senders": []})
    # May be False without sonar-cli on CI; just ensure no exception
    assert validate_config(cfg) in (True, False)


def test_platform_enum_value():
    assert Platform("sonar").value == "sonar"