"""API server adapter must honor the gateway connect contract."""

import inspect

from gateway.platforms.api_server import APIServerAdapter
from gateway.platforms.base import BasePlatformAdapter


def test_api_server_connect_accepts_is_reconnect_kwarg():
    signature = inspect.signature(APIServerAdapter.connect)
    base_signature = inspect.signature(BasePlatformAdapter.connect)

    assert "is_reconnect" in signature.parameters
    assert signature.parameters["is_reconnect"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["is_reconnect"].default is False
    assert signature.parameters["is_reconnect"].kind is base_signature.parameters["is_reconnect"].kind
