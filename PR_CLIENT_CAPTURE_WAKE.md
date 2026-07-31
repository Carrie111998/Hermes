# feat: client-capture wake word for remote desktop

## Summary

Remote Hermes backends (Docker / headless VM / machine in another room) often
have **no microphone**. Stock wake word opens PortAudio on the **server**, so
the desktop ear fails with:

> Failed to open the wake-word microphone.

This PR keeps **detection on the backend** (openWakeWord / sherpa / porcupine
unchanged) and adds **client capture**: the desktop streams 16 kHz mono int16
PCM over the existing authenticated WebSocket via a new `wake.feed` RPC.

That is the correct product fix for “agent remote + Mac mic hands-free.”

## Design

| Step | Where |
|------|--------|
| Arm ear (`wake.start`, `surface: gui`, `client_capture: true`) | Desktop → backend |
| Backend chooses `capture: client` when preferred / configured | `tools.wake_word.resolve_capture_mode` |
| Engine listens on an in-process PCM queue (no PortAudio device) | `WakeWordDetector(external_audio=True)` |
| Desktop `getUserMedia` → resample → `wake.feed` frames | `apps/desktop/src/lib/wake-client-capture.ts` |
| Phrase detected → `wake.detected` (unchanged) | `tui_gateway` |
| Stop client feeder so voice can take the mic | wiring on `wake.detected` |
| After voice, re-arm + restart feeder | `resumeWakeAfterVoice` |

Config:

```yaml
wake_word:
  capture: auto   # auto | local | client
```

- **auto** + desktop `client_capture: true` → client mode (remote-friendly)
- **auto** without prefer → local (CLI/TUI unchanged)
- **local** / **client** force the mode

## Test plan

- [x] `pytest tests/tools/test_wake_word.py` — 26 passed
- [ ] Desktop remote to headless backend: ear on → no “Failed to open mic”
- [ ] macOS mic permission prompt once; say “hey hermes” → voice session starts
- [ ] After voice ends, ear re-arms without a manual toggle
- [ ] Local (non-remote) backend with a real mic still works (`capture: local` / auto)
- [ ] CLI `/wake on` still uses local PortAudio (no client_capture)

## Notes for reviewers

- Client capture intentionally does **not** move the ONNX engine into Electron;
  only PCM transport moves. Smaller desktop footprint, shared engines with TUI.
- PCM stays on the desktop↔backend WebSocket; no third-party wake API.
- Older desktops without this feeder still get the old local-mic path.

Closes: remote wake on headless hosts (user report: hermes-migrate / vm-1).
