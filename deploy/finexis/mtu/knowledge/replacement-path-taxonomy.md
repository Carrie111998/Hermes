# MTU P0 — Replacement-Path Taxonomy

The "old category → new category" replacement paths the BOR must handle, derived from Melody's 18 example filenames (each names its ROP path) + the read cases. This is the taxonomy Melody's spec refers to as the "BOR table showing the replacement paths." **Melody confirms/extends this before the agent drafts live.**

`[NEW]` = case Melody marked as a newer reference example; `[OLD]` = superseded example kept for reference.

| # | Example case | Old category → New category | Notes |
|---|---|---|---|
| 01 | Voyage25, EliteTerm & SL Shield | ILP (with coverage) **or** WL → **Term** (EliteTerm); **Shield → Shield** | multi-recommendation case (term + shield + ILP) |
| 02 | Voyage25, EliteTerm (LimitedPay) & BandAid | WL → **Term** (EliteTerm); Endowment **or** ILP (without coverage) → **ILP** (Voyage); **PA → PA** | |
| 03 | Abundance | ILP (without coverage) → **ILP** (Abundance) | |
| 13 | SavvyInvestII & Voyage15 | ILP (without coverage) → **ILP** (Voyage15) | |
| 16 | HSBC Shield, HSBC Term Protector, Voyage25 & Singlife Acc. Care | WL & MultiPay Term → **Term** (ROPTermP); **Shield → Shield** | |
| 17 | iFast, EliteTerm | **Term → Term** (EliteTerm); PA → PA (BandAid); + investment (iFast) | cleanest term-to-term case |
| 18 | HSBC Diamond IUL | Term → **IUL** (Indexed Universal Life) | |
| 04* | Voyage25 (Franklin Income) + Premium above $36k declarations | ILP; flags **premium > $36k** declaration path | image-only (flagged) |
| 05*/09* | Singlife Accident Care / Careshield Plus + Accident Care | Accident & Health | image-only (flagged) |
| 06*/07*/08* | Navigator Wrap / Non-Wrap Accounts (CPF-OA / Cash) | Investment platform / wrap account | 07, 08 readable; 06 image-only |
| 10*/11* | FlexiProtector / Advisor's Own Case | (protection / advisor's own) | image-only (flagged) |
| 12 | Dependant Cover, Singlife My Whole Life Choice | WL (dependant cover) | readable |
| 14*/15* | HSBC Indexed Flexi Income / Singlife Flexi Life Income | Income / annuity-style | image-only (flagged) |

`*` = image/scanned PDF, content not machine-read (see GROUNDING.md).

## Category vocabulary (reused from bor-scraper analyzer.ts)

**Product categories:** Whole Life · Term Life · Investment-Linked (ILP) · Endowment · Personal Accident (PA) · Integrated Shield Plan (Shield/ISP) · Critical Illness · Health/Medical · CareShield/ElderShield · Retirement/Annuity · Disability Income · Indexed Universal Life (IUL) · Investment platform / Wrap account.

**Insurers seen:** AIA, Great Eastern, Prudential, NTUC/Income, Manulife, AXA, Aviva, Singlife, FWD, Tokio Marine, MSIG, Etiqa, China Life, Raffles Health, Sun Life, Zurich, HSBC Life, China Taiping, Transamerica. (Melody's unit uses Singlife, HSBC Life, AIA, Prudential, Manulife, iFast heavily.)

## How the agent uses this
- On intake, classify the case's **old→new path** from the existing plan + proposed plan categories.
- The path drives which ROP wording applies (e.g. "from ILP without coverage" vs "Shield to Shield" have different emphasis) and which checks fire (ILP path → CKA/fund/risk checks).
- Unknown / novel path → the agent asks rather than forcing a fit, and flags for Melody.

> **Open item for Melody:** confirm the canonical category list + any replacement paths this set doesn't cover, and whether the accident/health/income cases (04–06, 09, 14, 15) need their own BOR wording distinct from the ROP protection/investment cases.
