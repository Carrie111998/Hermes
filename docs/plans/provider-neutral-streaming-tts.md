# Provider-neutral streaming TTS

## Goal

Make Hermes speech playback smooth and model-independent while preserving the
existing low-latency text-to-speech experience. A provider adapter may emit
audio with arbitrary HTTP chunk boundaries, but the Hermes gateway and Desktop
must exchange a stable, versioned stream and play it from one continuous audio
clock.

Fish Audio S2 Pro is the first non-built-in provider required to conform to the
contract. The contract must remain useful when that provider is replaced.

## Terms

- **provider chunk**: arbitrary bytes yielded by a provider SDK or HTTP body.
- **audio frame**: validated mono signed-16-bit PCM covering a known duration.
- **playout buffer**: client-owned queued audio measured in milliseconds.
- **underrun**: the audio clock requests samples while no playable samples are
  buffered and the stream has not ended.
- **realtime factor (RTF)**: synthesis wall time divided by emitted audio time.

## Contract

The `/api/audio/speak-stream` WebSocket is versioned as `hermes.audio.v1`.

The server sends a JSON `start` frame before audio with:

- protocol version;
- encoding (`pcm_s16le`), sample rate, and channel count;
- monotonically increasing stream identifier;
- recommended initial and maximum playout-buffer durations.

Audio remains binary PCM for a narrow hot path. Each binary message is a whole,
sample-aligned audio frame. A JSON `audio` metadata frame precedes it and carries
the sequence number, starting sample offset, frame sample count, and synthesis
timing. JSON `end`, `error`, and `fallback` frames terminate explicitly. Client
`stop` and disconnect cancel production promptly.

The gateway owns normalization from provider chunks to 20 ms audio frames,
sequence/sample accounting, bounded buffering, and stream telemetry. Provider
adapters own format decoding and must yield raw PCM plus a declared format;
they do not own Desktop scheduling.

The Desktop owns playout. It buffers frames in a bounded ring, starts only after
an adaptive initial target is reached (or the stream ends), and feeds one
continuous audio clock. It increases the target after underruns and cautiously
decreases it after stable playback. It never creates one WebAudio source per
provider chunk.

## Degradation policy

- Short jitter is absorbed by the adaptive buffer.
- Missing/duplicate/out-of-order frames are detected from sequence/sample
  metadata and reported as a stream error, never silently reordered.
- If production is persistently slower than playback (RTF greater than one),
  buffering is bounded. Hermes may wait for a phrase to complete or use the
  existing whole-response fallback; it must not accumulate unbounded latency.
- Barge-in stops the audio clock and cancels upstream synthesis.
- Older gateways remain supported through the current raw-PCM compatibility
  path until the versioned `start` frame is observed.

## Testing seams

1. Pure Python framing tests: arbitrary provider chunk boundaries become exact
   20 ms aligned frames with monotonic sequence and sample offsets.
2. WebSocket contract tests: start/audio/binary/end ordering, cancellation,
   bounded queue behavior, fallback, and error termination.
3. Pure TypeScript playout-controller tests: startup threshold, ordering,
   underrun adaptation, bounded latency, drain, and cancellation using a fake
   audio sink and clock.
4. Desktop integration tests: version negotiation and legacy compatibility.
5. Qualification probe: exact Hermes-style request reports time-to-first-audio,
   RTF, p95/max inter-frame gap, underruns predicted at the configured buffer,
   cancellation latency, and concurrency behavior.
6. Live acceptance: Fish through the authenticated front door and a Hermes
   Desktop connected to the Beelink, without changing protected ingress merely
   to reload configuration.

## Acceptance

- Provider HTTP chunking cannot change the Desktop playback unit.
- Audio frame sequence and sample offsets are monotonic and loss-detectable.
- Desktop uses a continuous sink with an adaptive, bounded playout buffer.
- No audible gap is introduced at phrase boundaries when buffered audio exists.
- Cancellation cuts local playback immediately and stops server production.
- Legacy server compatibility and the existing whole-audio fallback remain.
- Qualification evidence distinguishes TTFA, throughput, jitter, and playout
  underruns; a provider with sustained RTF above one cannot be labelled realtime.
- Fish conforms through the same provider-neutral path without Fish-specific
  scheduling logic in Desktop or the WebSocket protocol.

## Delivery slices

1. Provider-neutral frame model and deterministic framing tests.
2. Versioned gateway transport, cancellation, telemetry, and WebSocket tests.
3. Continuous Desktop playout controller and adaptive buffering tests.
4. Fish adapter conformance, qualification probe, end-to-end evidence, and
   separately governed production promotion.
