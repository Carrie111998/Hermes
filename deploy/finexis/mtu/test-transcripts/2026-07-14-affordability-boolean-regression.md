# MTU affordability boolean regression — staged production-stack replay

Date: 2026-07-14 SGT  
Environment: temporary `HERMES_HOME`, real Hermes `GatewayRunner._handle_message` path, `gpt-5.4-mini`  
Data: synthetic only  
Live Telegram bot: unchanged

## Contract under test

- Ask only: “Do the client’s total annual premiums exceed 50% of annual income? Yes or no.”
- Never request or print income, surplus, net-worth, or affordability arithmetic.
- YES must state that premiums exceed the threshold and include the sustainability caution.
- NO must state that premiums do not exceed the threshold.

## Results

### ROP — threshold YES

The synthetic case supplied `yes`, while deliberately withholding only the client rationale and ROP acknowledgement. The first assistant turn asked only for those two missing facts. The completed BOR included exactly:

> The client’s total annual premiums exceed 50% of annual income, and the client was advised to consider the sustainability of the premium commitment.

It did not request or print an income or surplus figure.

### Non-ROP — threshold NO

The complete synthetic case supplied `no`. The assistant drafted immediately and included exactly:

> The client’s total annual premiums do not exceed 50% of annual income.

It included no ROP language and did not request or print an income or surplus figure.

### Missing threshold answer

The complete non-ROP case omitted only the affordability boolean. The assistant returned only:

> Do the client’s total annual premiums exceed 50% of annual income? Answer yes or no.

## First-run defect caught before ship

The first staged replay inverted the ROP `yes` input and wrote “do not exceed.” Source was tightened to an exact boolean-to-sentence mapping and the full replay was rerun. The results above are from the passing rerun.
