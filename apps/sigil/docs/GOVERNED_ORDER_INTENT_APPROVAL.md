# Governed Order Intent and Approval

## Overview

Sigil Step 22 transforms approved analytical portfolio-rebalancing
proposals into governed order-intent packages.

This step does not place orders, communicate with broker execution APIs,
or grant execution authority.

Order intents remain analytical artifacts requiring explicit downstream
approval and additional governed execution controls.

## Objectives

Step 22 provides:

- deterministic conversion of rebalance proposals into order intents;
- policy-based order-type and time-in-force controls;
- buying-power and sellable-quantity checks;
- per-order and aggregate notional constraints;
- immutable evidence and rationale references;
- deterministic package and approval identities;
- explicit approval, rejection, and expiration records;
- comparison of separate order-intent packages;
- a strict boundary between approval and execution.

## Source Package Requirements

The engine consumes a Step 21 `RebalancePackage`.

The source package must:

- contain a non-empty package identity;
- contain a source target-package identity;
- remain analytical only;
- have no execution authority.

Blocked source packages cannot produce actionable order intents.

A no-action source package creates a no-action order-intent package.

## Order Intent Construction

Each BUY or SELL rebalance proposal may become an `OrderIntent`.

Each intent records:

- source rebalance package identity;
- deterministic source-proposal identity;
- symbol, issuer, and sector;
- BUY or SELL side;
- order type;
- time in force;
- quantity;
- reference price;
- notional value;
- optional limit price;
- rationale;
- evidence references;
- policy constraints;
- blockers and warnings.

HOLD proposals are not convertible into order intents.

## Policy Controls

`OrderIntentPolicy` governs:

- minimum and maximum order notional;
- maximum intent count;
- maximum aggregate buy notional;
- maximum aggregate sell notional;
- maximum aggregate turnover;
- market-order availability;
- limit-order availability;
- required limit prices;
- permitted time-in-force values;
- fractional-share handling;
- evidence requirements;
- decimal precision.

The active policy is captured in each package as a deterministic snapshot.

## Account Capacity

`AccountCapacity` provides:

- available buying power;
- symbol-level sellable quantities.

The engine blocks a package when aggregate BUY notional exceeds available
buying power.

The engine also blocks SELL intents whose quantity exceeds the declared
sellable quantity for the symbol.

Account capacity is an analytical input and does not represent a live broker
reservation or execution guarantee.

## Package Status

An order-intent package may have one of these statuses:

- `READY_FOR_APPROVAL`
- `BLOCKED`
- `NO_ACTION`

A package is ready for approval only when:

- at least one intent exists;
- no package or intent blocker exists;
- all required policy constraints pass;
- buying-power checks pass;
- sellable-quantity checks pass.

## Approval Boundary

A ready package may produce an `ApprovalRequest`.

Approval requests summarize:

- the package under review;
- the intent identities;
- aggregate buy and sell notionals;
- aggregate turnover;
- constraint results;
- evidence references;
- required approver role;
- creation and optional expiration timestamps.

Approval decisions produce immutable `ApprovalRecord` objects.

Supported outcomes include:

- approved;
- rejected;
- expired.

Approval authorizes only downstream consideration of the package.

Approval does not:

- submit an order;
- reserve funds;
- contact a broker;
- bypass later risk controls;
- create execution authority;
- guarantee execution.

## Deterministic Audit Identities

Canonical JSON and SHA-256 identities are used for:

- source proposals;
- order intents;
- order-intent packages;
- approval requests;
- approval records.

Equivalent normalized payloads produce equivalent identities.

Payload mutation causes identity verification to fail.

## Comparison

`compare_order_intent_packages` identifies:

- symbols added or removed;
- side changes;
- quantity changes;
- notional changes;
- order-type changes;
- limit-price changes;
- status changes;
- blocker changes;
- policy changes.

This allows human reviewers and downstream governed systems to understand
what changed between package revisions.

## Security and Governance

Step 22 intentionally excludes:

- broker credentials;
- broker sessions;
- live market-order submission;
- live limit-order submission;
- cancellation or replacement;
- unrestricted network execution;
- automatic approval;
- automatic spending;
- execution authority.

Later execution steps must independently validate approval, freshness,
account state, market state, policy state, and authorization before any
real-world action is considered.
