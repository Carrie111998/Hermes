// Strict classification for the zombie-stream (half-open gRPC) liveness probe.
//
// spectrum-ts only reconnects when its inbound async iterator throws or ends.
// A half-open ("zombie") socket makes the iterator hang forever — no error,
// no end — so inbound silently dies while /healthz still looks fine. The
// adapter periodically drives a cheap authenticated unary read over the same
// channel. STRICT semantics:
//
//   - probe resolves, or rejects with a not-found-shaped error for our
//     synthetic id            -> ALIVE (the wire round-tripped)
//   - probe rejects any other way (UNAVAILABLE, DEADLINE_EXCEEDED, network
//     down, ...)              -> INCONCLUSIVE — never treated as alive, and
//                                never treated as zombie-proof either
//
// A successful unary call proves only that the channel can round-trip. It does
// NOT prove that a quiet event stream is dead: healthy shared lines can emit no
// messages for hours. The Python adapter therefore treats success as healthy
// and only restarts after repeated probe hangs.
//
// This helper is pure (no SDK, no timers) so tests can execute it under
// node — see tests/plugins/platforms/photon/test_zombie_stream_watchdog.py.

// gRPC NOT_FOUND is code 5; SDKs also surface it as "not found" / "NotFound"
// message text. Anything not clearly not-found is inconclusive.
const NOT_FOUND_RE = /not[\s_-]?found/i;

/**
 * Classify the rejection of the synthetic-id probe read.
 *
 * @param {unknown} err error thrown by `space.getMessage(<synthetic id>)`
 * @returns {{alive: boolean, inconclusive: boolean, reason: string}}
 */
export function classifyProbeRejection(err) {
  const code = err && typeof err === "object" ? err.code : undefined;
  const message =
    err && typeof err === "object" && err.message
      ? String(err.message)
      : String(err);
  if (code === 5 || code === "notFound" || NOT_FOUND_RE.test(message)) {
    // Expected: the synthetic id doesn't exist. The unary call completed a
    // round-trip, so the channel is provably alive.
    return { alive: true, inconclusive: false, reason: "not-found round-trip" };
  }
  // Anything else (UNAVAILABLE, DEADLINE_EXCEEDED, TLS, auth, ...) does NOT
  // prove liveness — and doesn't prove a zombie either.
  return { alive: false, inconclusive: true, reason: message };
}
