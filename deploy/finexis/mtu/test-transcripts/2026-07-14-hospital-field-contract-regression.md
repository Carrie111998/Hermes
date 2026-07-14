# MTU hospital field-contract regression — staged production-stack replay

Date: 2026-07-14 SGT  
Environment: temporary `HERMES_HOME`, real Hermes `GatewayRunner._handle_message` path, `gpt-5.4-mini`  
Data: synthetic only  
Live Telegram bot: unchanged

## Contract under test

- Integrated Shield/hospitalisation cases use exact base-plan and exact/versioned rider names as the BOR identifiers.
- The assistant never asks for or prints rider benefit limits, deductible, or coinsurance.
- A missing or ambiguous rider triggers only an exact/versioned rider-name clarification.
- Standard alternatives and the Amelia-approved replacement declaration are inserted without asking.
- Explicit contradiction or uncertainty suppresses the replacement declaration and escalates to Melody.
- The Shield exception does not silently extend to CareShield, accident, or other A&H categories.
- Protection/life cases retain their material-figure intake.

## Passing staged cases

1. Shield ROP, complete input
   - Drafted immediately from exact existing/proposed plan and rider names.
   - Asked for none of the prohibited hospital-plan figures.
   - Included exactly: “The client was advised of the replacement disadvantages and wishes to proceed.”
   - Inserted the standard Shield alternatives and replacement-options declarations without a confirmation question.

2. Shield non-ROP, complete input
   - Drafted immediately from exact plan/rider name, premium, and supplied rationale.
   - Asked for none of the prohibited figures.
   - Excluded all ROP acknowledgement and replacement language.

3. Shield ROP with explicit contradiction
   - Suppressed the standard replacement declaration.
   - Returned a concise Melody escalation instead of a BOR.

4. Shield with ambiguous rider
   - Asked only for the exact/versioned rider name.
   - Did not ask for benefit limits, deductible, or coinsurance.

5. CareShield negative control
   - Did not inherit the Integrated Shield field contract.
   - Refused to draft and escalated because CareShield has no approved category contract yet.

6. Term-life negative control
   - Continued to request material protection figures: sum assured, premium, and coverage term.

## First-run defects caught before ship

- The initial unsupported-category rule let a CareShield case draft from generic assumptions. It now fails closed to Melody until its scanned corpus is encoded.
- An explicit ROP contradiction initially produced a holding draft. It now returns only an escalation note.

## Separate pre-DEBUT defect surfaced

The staged model still emitted Markdown markers and closing offers despite the existing plain-text output contract and an added final-response self-check. This is not a hospital-field classification failure, but it is not demo-ready. It requires deploy-owner/runtime investigation under the active pre-DEBUT lane; no runtime change was made here.
