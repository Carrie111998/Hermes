"""Regression: WSL voice detection must honor PIPEWIRE_REMOTE, not only PULSE_SERVER.

detect_audio_environment() honors forwarded audio (has_forwarded_audio =
PULSE_SERVER or PIPEWIRE_REMOTE or a reachable socket) in the SSH and container
blocks, but the WSL block previously checked only PULSE_SERVER — so a WSL user
with PipeWire forwarding (PIPEWIRE_REMOTE) was wrongly blocked from voice mode.
These tests mock /proc/version so they reproduce the WSL path on any host.
"""
import builtins
import io
from unittest.mock import MagicMock

WSL = "Linux version 5.15.0-microsoft-standard-WSL2 (oe-user@oe-host)"


def _force_wsl(monkeypatch, content=WSL):
    real_open = builtins.open
    def fake_open(file, *a, **k):
        if str(file) == "/proc/version":
            return io.StringIO(content)
        return real_open(file, *a, **k)
    monkeypatch.setattr(builtins, "open", fake_open)


def _base(monkeypatch):
    for v in ("SSH_CLIENT", "SSH_TTY", "SSH_CONNECTION", "PULSE_SERVER"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.delenv("PIPEWIRE_REMOTE", raising=False)
    monkeypatch.setattr("hermes_constants.is_container", lambda: False)
    monkeypatch.setattr("tools.voice_mode._pulse_socket_reachable", lambda: False)
    # The WSL2 PowerShell TTS fallback makes the no-forwarding path host-
    # dependent (powershell.exe + ffmpeg on PATH). Pin it so these tests are
    # deterministic on any machine: the fallback is only exercised by the
    # dedicated test that mocks it to True.
    monkeypatch.setattr("tools.voice_mode._wsl_powershell_tts_available", lambda: False)
    sd = MagicMock(); sd.query_devices.return_value = [{"name": "dev"}]
    monkeypatch.setattr("tools.voice_mode._import_audio", lambda: (sd, MagicMock()))


def test_wsl_with_pipewire_remote_allows_voice(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("PIPEWIRE_REMOTE", "/run/user/1000/pipewire-0")
    _force_wsl(monkeypatch)
    from tools.voice_mode import detect_audio_environment
    result = detect_audio_environment()
    assert result["available"] is True, result["warnings"]


def test_wsl_with_pulse_server_still_allows_voice(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("PULSE_SERVER", "unix:/mnt/wslg/PulseServer")
    _force_wsl(monkeypatch)
    from tools.voice_mode import detect_audio_environment
    assert detect_audio_environment()["available"] is True


def test_wsl_without_forwarding_still_blocks(monkeypatch):
    _base(monkeypatch)
    _force_wsl(monkeypatch)  # no PULSE_SERVER, no PIPEWIRE_REMOTE
    from tools.voice_mode import detect_audio_environment
    res = detect_audio_environment()
    assert res["available"] is False
    assert any("WSL" in w for w in res["warnings"])


def test_wsl_without_forwarding_but_powershell_fallback_allows_tts(monkeypatch):
    """The WSL2 PowerShell TTS fallback relaxes the no-forwarding block for
    OUTPUT (TTS playback) while keeping recording guidance visible.

    Regression for the stale-test bug: this path was added by the PowerShell
    fallback feature but never unit-tested, so a WSL host with powershell.exe
    and ffmpeg (the fallback's only preconditions) flipped the no-forwarding
    test from pass to fail depending on the machine.
    """
    _base(monkeypatch)
    _force_wsl(monkeypatch)  # no PULSE_SERVER, no PIPEWIRE_REMOTE
    from tools.voice_mode import detect_audio_environment
    monkeypatch.setattr("tools.voice_mode._wsl_powershell_tts_available", lambda: True)
    res = detect_audio_environment()
    # TTS playback works via Media.SoundPlayer on the Windows host, so voice
    # mode is available — but the WSL guidance notice must still surface.
    assert res["available"] is True
    assert any("WSL" in w for w in res["notices"])
