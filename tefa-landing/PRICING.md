# Pricing methodology (internal — not published to customers)

Every price on the storefront mockups is generated from a purchase cost, not picked by
feel, per the request to price off what we actually pay. The formula:

```
price = ceil(cost × MARKUP) − 0.05        # MARKUP = 2.2 by default
```

`MARKUP = 2.2` is a placeholder — a standard general-retail multiplier that assumes cost
covers only the item itself, and the markup absorbs everything else: inbound shipping,
packaging and the project book printed into every kit, payment processing fees, an
allowance for returns/defects, and margin. **Replace the cost column below with what you
actually pay your supplier per item**, and adjust `MARKUP` to whatever multiple you want
margin to sit at — the formula recalculates every price at once.

**This file is for internal use.** The storefront itself only ever shows the final price,
the way any normal retailer does — customers don't see your cost or your markup. Showing
the cost/margin math on the public page would be unusual for retail and just invites
scrutiny of your margin from customers or from the ESA program auditing the vendor. If you
want a different markup for different departments (say, thinner margin on curriculum,
fatter on hardware), split `MARKUP` per department below instead of one global number.

## Example cost table (illustrative — replace before publishing)

| SKU | Product | Example cost | Price shown to customer | Margin |
|---|---|---:|---:|---:|
| MMM-PR-101 | Home & Repair Tool Roll | $38.00 | $83.95 | 55% |
| MMM-PR-102 | Money & First Job Kit | $22.00 | $48.95 | 55% |
| MMM-PR-103 | Kitchen & Provision Kit | $27.00 | $59.95 | 55% |
| MMM-SC-201 | Situation Handling Deck | $14.00 | $30.95 | 55% |
| MMM-SC-202 | Focus & Energy System | $24.00 | $52.95 | 55% |
| MMM-SC-203 | Self-Advocacy Workbook | $11.00 | $24.95 | 56% |
| MMM-CS-301 | Graphic Design Bench | $145.00 | $318.95 | 55% |
| MMM-CS-302 | Motion & Video Kit | $165.00 | $363.95 | 55% |
| MMM-CS-303 | Skill-to-Income Pack | $16.00 | $35.95 | 55% |
| MMM-AT-401 | AI Literacy Bench Kit | $42.00 | $92.95 | 55% |
| MMM-AT-402 | Electronics & Robotics Starter | $58.00 | $127.95 | 55% |
| MMM-AT-403 | 3D Design & Fabrication Intro | $210.00 | $462.95 | 55% |
| MMM-HS-501 | Core Subjects Workbook Set | $32.00 | $70.95 | 55% |
| MMM-HS-502 | Homeschool Assessment & Portfolio Kit | $26.00 | $57.95 | 55% |
| MMM-HS-503 | Daily Supply Restock Box | $19.00 | $41.95 | 55% |
| MMM-HS-504 | Art & Craft Foundations Kit | $21.00 | $46.95 | 55% |

Every "example cost" above is a guess for demonstration purposes, not a real quote from
any supplier — swap in what you actually pay before these numbers go anywhere near a
customer. Note the three highest-cost items (design tablet, motion kit, 3D bench) are the
ones where a fixed 2.2× multiplier produces a steep dollar jump — worth checking those
three specifically against what a family would expect to pay before publishing, even
though the margin percentage is identical to the cheaper items.

## Recomputing

```
python3 - <<'PY'
import math
MARKUP = 2.2
def price(cost):
    return math.ceil(cost * MARKUP) - 0.05
print(price(38.00))   # -> 83.95
PY
```
