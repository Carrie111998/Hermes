# Governed Broker Submission and First-Launch Certification

Sigil Step 33 establishes the final governed boundary between a READY live
execution handoff and the existing hardened broker execution provider.

## Safety properties

- Requires a fresh Step 32 handoff and immutable execution envelope.
- Requires explicit owner confirmation and launch certification references.
- Defaults to a maximum first-launch order notional of **$25**.
- Blocks immediately when the kill switch is active.
- Consumes each execution envelope at most once.
- Calls the supplied broker submitter no more than once.
- Records broker acceptance and broker rejection as immutable receipts.
- Treats timeout or ambiguous transport failure as `outcome_uncertain`.
- Never permits automatic retries after an uncertain outcome.
- Preserves evidence from admission, handoff, submission, and broker response.

## Integration boundary

The module does not add a second HTTP client. Production wiring must pass the
validated envelope into the existing governed Public execution provider, which
already owns authentication, preflight, approval consumption, transport
allowlisting, reconciliation, and audit evidence.

## First-launch posture

Step 33 certifies the controlled submission boundary. It does not authorize
unattended capital scaling. The owner remains the final authority, the initial
capital limit remains policy-bound, and any uncertain broker outcome requires
reconciliation before another submission can be considered.
