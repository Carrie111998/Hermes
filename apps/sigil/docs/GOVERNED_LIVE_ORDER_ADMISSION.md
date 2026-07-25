# Governed Live Order Admission

Sigil Step 31 evaluates each proposed live order against an armed Step 30
launch-control artifact.

The admission gate verifies:

- launch control is currently armed
- exact broker and account binding
- allowed asset class, symbol, and order type
- positive quantity and notional
- per-order notional ceiling
- cumulative live-capital ceiling
- daily realized-loss ceiling
- maximum open-position count
- replay and duplicate protection
- operator authorization reference
- market-data freshness
- immutable evidence

Admission states are `rejected`, `admitted`, `duplicate`, `expired`, and
`suspended`.

This milestone creates an order-admission artifact only. It does not connect to
a broker, submit an order, or move money.
