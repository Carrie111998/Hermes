"""Regression tests for gateway-owned TTS autoplay."""

import json

import agent.tool_executor as executor


def test_successful_tts_result_starts_afplay(monkeypatch, tmp_path):
    audio = tmp_path / "reply.mp3"
    audio.write_bytes(b"audio")
    calls = []

    class FakeProcess:
        pid = 123

        def poll(self):
            return 0

    monkeypatch.setattr(executor.shutil, "which", lambda name: "/usr/bin/afplay")
    monkeypatch.setattr(
        executor.subprocess,
        "Popen",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or FakeProcess(),
    )
    executor._tts_playback_process = None

    executor._autoplay_tts_result(
        "text_to_speech",
        json.dumps({"success": True, "file_path": str(audio)}),
    )

    assert calls == [
        (
            ["/usr/bin/afplay", str(audio)],
            {
                "stdin": executor.subprocess.DEVNULL,
                "stdout": executor.subprocess.DEVNULL,
                "stderr": executor.subprocess.DEVNULL,
                "start_new_session": True,
            },
        )
    ]


def test_unsuccessful_tts_result_does_not_play(monkeypatch):
    called = []
    monkeypatch.setattr(executor.subprocess, "Popen", lambda *a, **k: called.append((a, k)))

    executor._autoplay_tts_result(
        "text_to_speech", json.dumps({"success": False, "error": "provider down"})
    )

    assert called == []


def test_other_tools_do_not_play(monkeypatch):
    called = []
    monkeypatch.setattr(executor.subprocess, "Popen", lambda *a, **k: called.append((a, k)))

    executor._autoplay_tts_result("read_file", "not tts")

    assert called == []
