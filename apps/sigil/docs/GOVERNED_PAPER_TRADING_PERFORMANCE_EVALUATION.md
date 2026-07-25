# Governed Paper Trading Performance Evaluation

Sigil Step 26 evaluates completed paper trading sessions without placing orders
or introducing live brokerage access.

## Inputs

The evaluator consumes:

- a terminal Step 25 paper trading session
- explicit trade outcomes
- an explicit equity curve
- evaluation policy thresholds
- evaluator identity and evidence references

## Metrics

The deterministic report records:

- trade count
- winning, losing, and breakeven trade counts
- win rate
- net profit and loss
- gross profit and gross loss
- profit factor
- maximum drawdown
- ending equity
- fees
- compliance score
- passed and failed policy checks

## Governance

The evaluator returns one recommendation:

- `pass`: all policy and evidence checks passed
- `review`: performance thresholds failed but core integrity checks passed
- `fail`: certification, evidence, or accounting integrity failed

The evaluator rejects non-terminal sessions, duplicate trade identities, and
missing equity curves. It verifies that trade-level profit and loss agrees with
the session-level recorded result.

This milestone does not place trades, connect to a broker, or authorize live
capital.
