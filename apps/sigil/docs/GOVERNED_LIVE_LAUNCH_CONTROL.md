# Governed Live Launch Control

Sigil Step 30 converts an active Step 29 certification into a time-bounded,
operator-approved launch-control artifact.

The gate verifies:

- active and unexpired certification
- exact broker and account binding
- asset-class, order-type, and symbol scope
- operator launch approval
- armed kill switch
- confirmed rollback path
- one-time authorization reference
- live-capital and per-order ceilings
- maximum daily loss
- maximum open positions
- certification-aligned launch window
- immutable evidence

Launch-control states are `blocked`, `armed`, `suspended`, and `expired`.

This milestone does not connect to a broker, submit orders, or move money. It
only creates the governed state that a later execution-admission layer may
consume.
