from pathlib import Path
from types import SimpleNamespace

from gateway.config import GatewayConfig, load_gateway_config
from gateway.run import GatewayRunner


def test_stt_echo_transcripts_defaults_on_for_backwards_compatibility():
    cfg = GatewayConfig.from_dict({})

    assert cfg.stt_enabled is True
    assert cfg.stt_echo_transcripts is True
    assert cfg.stt_echo_format == "legacy"
    assert cfg.to_dict()["stt_echo_transcripts"] is True
    assert cfg.to_dict()["stt_echo_format"] == "legacy"


def test_top_level_stt_echo_transcripts_takes_precedence():
    cfg = GatewayConfig.from_dict({
        "stt_echo_transcripts": False,
        "stt": {"echo_transcripts": True},
    })

    assert cfg.stt_echo_transcripts is False


def test_nested_stt_echo_format_enables_copyable_transcript_block():
    cfg = GatewayConfig.from_dict({"stt": {"echo_format": "transcript_md"}})
    runner = object.__new__(GatewayRunner)
    runner.config = cfg

    assert cfg.stt_echo_format == "transcript_md"
    assert runner._format_stt_transcript_echo("Как дела?") == (
        "```\n"
        "Как дела?\n"
        "```"
    )


def test_unknown_stt_echo_format_falls_back_to_legacy():
    cfg = GatewayConfig.from_dict({"stt": {"echo_format": "unknown"}})

    assert cfg.stt_echo_format == "legacy"


