# MTU BOR Assistant — Knowledge Base

This is the operating knowledge for the MTU (Melody Tan Unit / Finexis) BOR-generation assistant.
It is loaded into the system prompt (hermes reads $HERMES_HOME/SOUL.md). The constitution
(mtu_constitution.yaml) supplies identity + the bor_generation job instructions; this file supplies
the BOR checks table, replacement-path taxonomy, draft template, and the approved disclosures library.

---

## bor-required-checks.yaml

# MTU P0 — BOR Required Checks ("the BOR table")
#
# The checklist the MTU agent runs before drafting a BOR. Grounded in Melody's
# 18 real cases + the amelia-finexis bor-scraper encoded standard (see GROUNDING.md).
# HELD for Amelia; Melody confirms/edits this table before the agent drafts live.
#
# applies_when values:
#   always     - every BOR
#   rop        - only when it is a replacement of policy (switch)
#   ilp        - only when an investment-linked / investment product is involved
#   accident   - only for personal-accident product recommendations
#
# collect: what the agent must obtain from the advisor before drafting
# derive:  what the agent computes / states from collected inputs
# never:   guardrail — the agent must not do this

meta:
  version: v0-draft
  status: HELD_for_amelia_and_melody_review
  grounded_in: "18 OneDrive YFB cases (10 text-readable) + bor-scraper analyzer/pdf-extractor"
  note: >-
    Melody set BOR generation as P0. This is the FIRST version of the table for her
    to confirm; she may add must-check items before the agent drafts live BORs.

# ── Intake: the three things the advisor states up front ──────────────────────
intake:
  - id: existing_plan
    ask: "What plan(s) does the client currently have? (insurer, plan name, and category)"
    collect: [insurer, plan_name, category, sum_assured_by_benefit, premium, coverage_till_age]
    why: "The BOR must refer to the client's starting position clearly."
    applies_when: always
  - id: proposed_plan
    ask: "What plan is being proposed? (insurer, plan name + riders, and category)"
    collect: [insurer, plan_name, riders, category, sum_assured_by_benefit, premium_first_year, premium_subsequent, policy_term, min_investment_period]
    why: "The recommended product with its specifics anchors the whole BOR."
    applies_when: always
  - id: is_replacement
    ask: "Is this a replacement of policy (ROP)? If yes — what is being replaced, and with what?"
    collect: [is_rop, replaced_products, replacing_products, replaced_type_investment_or_insurance]
    why: "ROP cases need the extra replacement checks + more careful wording."
    applies_when: always

# ── Required checks (the compliant-BOR must-covers) ───────────────────────────
checks:
  - id: coverage_comparison
    ask: "How does coverage change for Death, TI, TPD, and CI — amount AND duration, before vs after?"
    derive: coverage_delta_statement   # "meets/does not meet client's needs for protection of family/assets, disability, CI"
    why: "The BOR must show the actual change in protection, not just product names. Shortfalls are stated, not hidden."
    grounded: "files 01, 17 — 'Total coverage of the plan meets/does not meet client's needs for…'"
    applies_when: always
  - id: premium_comparison
    ask: "Is the new plan cheaper or more expensive than the old plan? If higher, why is the increase justified?"
    derive: premium_movement_statement
    why: "The recommendation must explain the trade-off clearly."
    grounded: "files 01, 17 — 'premiums are lowered compared to previous planning' / justification when higher"
    applies_when: always
  - id: income_sustainability_threshold
    ask: "Is the new premium / investment budget more than 50% of the client's income (net worth / surplus)?"
    derive: sustainability_flag
    why: >-
      Key affordability check before finalising. The YFB carries a standing note:
      'If the budget you set aside is more than 50% of your net worth / surplus,
      you may wish to consider the sustainability of your premiums / investment amounts.'
    grounded: "files 03, 13, 17 — the 50% sustainability note"
    applies_when: always
  - id: alternatives_considered
    ask: "Were other products / methods considered? Which, and why were they not selected?"
    collect: [alternatives_considered, reason_recommended_over_alternatives]
    why: "The BOR must explain why the recommended product was chosen over alternatives."
    grounded: "files 01, 17 — 'After discussing the pros and cons of [WL/term/ILP/endowment/…], the client preferred X because…'"
    applies_when: always
  - id: reason_for_change
    ask: "Why is the client changing / why this recommendation? (in the client's own terms)"
    collect: [client_stated_rationale]
    why: "The draft must reflect the client's ACTUAL rationale — the agent must not invent one."
    grounded: "every readable ROP case — 'the reason for the switch/replacement is highlighted below:'"
    never: "invent a rationale the advisor did not state"
    applies_when: always
  - id: rop_disadvantages_acknowledged
    ask: "Confirm the client was advised of the replacement disadvantages and wishes to proceed."
    derive: rop_disadvantage_acknowledgement   # inserts the standard 4-point disadvantage disclosure (see standard-disclosures.md)
    why: "ROP compliance — client must be shown the downside of switching before proceeding."
    grounded: "files 01, 17 — the 4 general disadvantages + 'wish to proceed notwithstanding'"
    applies_when: rop
  - id: alternatives_to_replacement_explored
    ask: "Were other options to a full replacement explored — increasing sum assured, adding riders, converting the existing policy?"
    collect: [alternatives_to_replacement]
    why: "ROP declaration requires confirming replacement was chosen only after these were considered and it is best-interest."
    grounded: "file 01 — 'Other available options—such as increasing the sum assured, attaching riders, or converting the policy—have been explored.'"
    applies_when: rop
  - id: product_comparison_freshness
    ask: "Are the comparison products still available in market and is the comparison list current?"
    derive: freshness_flag
    why: "Comparisons must not be against outdated products. Melody flags a ~monthly refresh of the comparison list."
    grounded: "spec + Melody note"
    applies_when: always
  - id: reference_numbers
    ask: "Does this BOR need an application / reference number (e.g. ECIM)? If so, where does the advisor get it?"
    collect: [reference_numbers]
    why: "Some BOR flows require reference numbers."
    never: "invent a reference number — ask the advisor, or leave a clearly-marked placeholder"
    applies_when: always
  # ── ILP-specific checks (only when an investment-linked product is involved) ──
  - id: ilp_cka_risk_profile
    ask: "For the ILP: what was the CKA result (passed / not passed) and the client's risk profile?"
    collect: [cka_result, risk_profile]
    why: "ILP recommendations must reference CKA + risk profile for fund suitability."
    grounded: "file 01 — 'Client did not pass CKA and has a risk profile of aggressive.'"
    applies_when: ilp
  - id: ilp_fund_justification
    ask: "Which fund(s), and why does the fund's objective align with the client's goals?"
    collect: [fund_name, fund_objective_alignment]
    why: "Fund choice must be justified against the client's investment horizon and goals."
    grounded: "file 01 — fund recommended as objective aligns with long-term horizon"
    applies_when: ilp
  - id: ilp_risk_disclosures
    ask: "Confirm the ILP risk disclosures apply (non-guaranteed returns, market volatility, surrender charges timeline, premium holiday, welcome/start-up bonus)."
    derive: ilp_disclosure_block   # see standard-disclosures.md
    why: "ILP-specific caveats are mandatory in the BOR narrative."
    grounded: "files 01, 03, 13, 18"
    applies_when: ilp

# ── Output contract ───────────────────────────────────────────────────────────
output:
  produce:
    - "a WhatsApp-ready BOR draft (the recommendation-rationale narrative), per recommended product / need-bucket"
    - "a short note listing: which details were used, which fields were still missing, and anything to check with Melody"
  never:
    - "make product recommendations by itself"
    - "invent missing facts (SA, premium, reference numbers, client rationale)"
    - "treat an incomplete case as ready — ask for the missing checklist items first"
    - "give compliance sign-off — the draft is for ADVISOR review; unclear/sensitive cases escalate to Melody"

---

## replacement-path-taxonomy.md

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

---

## bor-draft-template.md

# MTU P0 — BOR Draft Template (recommendation-rationale narrative)

The structure the MTU agent fills when drafting a BOR. **Grounded in real cases (files 01, 17 read in full; pattern confirmed across the 10 text-readable cases).** The BOR is written **per recommended product / need-bucket** — a case with three recommendations (e.g. term + shield + ILP) produces three narrative blocks in this shape.

The template maps to the amelia-finexis bor-scraper's **boilerplate-vs-variable** model: `{{VARIABLE}}` slots are collected from the advisor (never invented); *[boilerplate]* lines are inserted from `standard-disclosures.md`.

---

## Block structure (per recommended product)

**1. Need being addressed.**
> To address client's {{NEED}} [e.g. protection needs for Death, TI, TPD and Critical Illness / hospitalisation protection with deductible & co-insurance / developing savings & investment needs] …

**2. Existing position + reason for the change** (the "reason for switch/replacement").
> Client currently has {{N}} plan(s) with {{INSURER(S)}}: {{EXISTING_PLANS with SA + premium + coverage-till-age}}.
> After a recent review, client felt {{CLIENT_STATED_RATIONALE}} [e.g. premiums are expensive for the coverage provided / coverage duration is insufficient and wants it extended / a needed feature is missing / no longer wants cash value in protection planning / wants premium flexibility].

**3. Alternatives considered → why the recommended product.**
> After discussing the pros and cons of {{ALTERNATIVES_CONSIDERED}} [e.g. whole life, term, and investment-linked plans / various insurers' shield plans / different personal-accident plans / endowment, investment, and annuity], the client preferred {{RECOMMENDED_CATEGORY}} due to {{REASON_OVER_ALTERNATIVES}}.

**4. Recommended product with specifics.**
> Client has chosen {{PROPOSED_PLAN_NAME + RIDERS}} as it matches client's affordability requirement.
> Sum Assured: {{SA_BY_BENEFIT}} (Death/TI/TPD; CI/ECI; multipay; etc.)
> Premiums: {{PREMIUM_FIRST_YEAR}} first year / {{PREMIUM_SUBSEQUENT}} subsequent [GST-inclusive where applicable]
> Coverage term: {{TERM}} (till age {{AGE}}, payable till age {{AGE}}) [ILP: Minimum Investment Period {{MIP}}]

**5. Coverage comparison (before → after).**
> {{COVERAGE_DELTA_STATEMENT}} [e.g. new coverage term is extended and now includes multipay CI; coverage is lowered but client does not require such high coverage after review].
> Total coverage of the plan {{MEETS / DOES NOT MEET}} client's needs for "protection of family and/or assets in the event of death", "protection for disability", and "protection for critical illness". {{IF_SHORTFALL: Shortfalls will be addressed in future.}}

**6. Premium comparison + justification.**
> {{PREMIUM_MOVEMENT_STATEMENT}} [e.g. premiums are lowered compared to previous planning] {{IF_HIGHER: justification}}.
> {{IF_SUSTAINABILITY_FLAG: note that budget exceeds 50% of surplus — advisor to confirm sustainability}}.

**7. ILP block (only when an investment product is recommended).**
> Client {{PASSED / DID NOT PASS}} CKA and has a risk profile of {{RISK_PROFILE}}.
> Fund: {{FUND_NAME}} — recommended as its investment objective aligns with the client's {{HORIZON/GOAL}}.
> *[welcome/start-up bonus note; premium-holiday note; surrender-charge timeline; non-guaranteed-returns + market-volatility caveat — from standard-disclosures.md]*

**8. Standard replacement disclosures** *(boilerplate — inserted, not written per-case; see `standard-disclosures.md`).*
> *[non-disclosure of pre-existing conditions; recommended after fact-find + needs-analysis + product comparison; FC may receive additional commission; cost-benefit comparison performed as this is a replacement of policy; 90-day waiting period may apply; may incur losses on premiums already paid; will lose existing coverage; pre-existing conditions may not be covered under the new policy]*

---

## Output the agent returns

1. The BOR draft (one or more narrative blocks in the shape above) — **copy-pasteable into the YFB "recommendation" section**.
2. A **coverage note** stating: fields used, fields still missing, and anything to check with Melody before use.

## Hard rules for the draft (from Melody's spec + the checks table)
- Do **not** invent SA, premiums, reference numbers, or the client's rationale. Missing → ask, or leave a clearly-marked `[[MISSING: …]]` placeholder.
- Do **not** make the product recommendation — the advisor decides; the agent drafts the justification for the advisor's chosen product.
- Draft is for **advisor review**; sensitive/unclear cases escalate to Melody.
- The narrative must give the advisor everything needed to complete the YFB "For Switching / Replacement of Policies" declaration (old→new mapping, disadvantages acknowledged, alternatives explored, suitability) — see `../GROUNDING.md`.

---

## standard-disclosures.md

# MTU P0 — Standard Disclosures Library (boilerplate)

The compliance boilerplate that recurs near-verbatim across the real BOR cases (files 01, 17; confirmed by the bor-scraper's 70%+-frequency boilerplate detection). These are **generic regulatory/compliance statements** (not client-specific) that the agent **inserts** into the draft rather than writing fresh per case. This keeps drafts consistent and compliant, and confines the agent's generative work to the variable, client-specific narrative.

> These are transcribed from the structure of the real cases as a **starting library** for Melody to confirm/adjust. Melody owns the final wording; the agent must use her approved phrasing, not improvise compliance language.

## A. Replacement / switching disadvantages (ROP cases) — insert when `is_rop = yes`

The four general disadvantages the client must be shown (from the "For Switching / Replacement of Policies" declaration):

1. May incur penalties / transaction costs for terminating the existing policy(ies) or investment product(s), without gaining any real benefit from replacing them.
2. Financial benefits accumulated over the years may be lost.
3. May be offered a lower level of benefit at a higher or same cost (or the same benefit at higher cost); the new policy may be less suitable.
4. If existing medical conditions are covered by the existing plan, coverage for those conditions may be lost.

Plus the standard acknowledgements seen in the narratives:
- Pre-existing medical conditions may not be covered under the new policy; a 90-day waiting period may apply before certain benefits take effect.
- The client may incur losses on premiums already paid on the existing policy, and will lose existing coverage on surrender.
- Other options — increasing the sum assured under the existing policy, attaching riders, or converting the policy — were explored; replacement was recommended only after assessment confirmed it suitable and in the client's best interest.
- The product was recommended after the Financial Consultant performed a cost-benefit comparison with the policy to be replaced, as this is a replacement of policy.

## B. General recommendation disclosures — insert on every BOR

- The client was informed that in the event of non-disclosure of any pre-existing medical conditions, the insurer has the right to not pay out benefits as stated if diagnosed due to pre-existing conditions.
- The product was recommended after fact-find, needs analysis, and product comparison.
- The client is aware that the Financial Consultant may receive additional commission for selling the recommended product.
- The client has agreed for soft copies of the documents to be electronically mailed after the company has processed them.

## C. ILP-specific disclosures — insert when an investment-linked product is involved

- The ILP comes with charges, investment risks, exposure to market volatility, and non-guaranteed returns; past performance is not an indication of future performance.
- Saving/investment needs may not be met and might be insufficient as returns are non-guaranteed and dependent on the future performance of the chosen fund; the client would like to address any remaining shortfall at a later date.
- Surrender charges apply if the client surrenders before the {{Nth}} year, reducing to 0% at the {{N+1}}th-year mark (per product summary).
- A welcome / start-up bonus applies on early-year premiums and can help hedge against market downturn.
- A premium-holiday feature is available after the {{Nth}} year, allowing the client to pause premiums under financial hardship; the client is aware this affects targeted returns on withdrawal/surrender.
- The client confirms no gift was provided, and any promotion offered did not influence the decision to purchase.

## Usage rules
- The agent **inserts** the relevant blocks (A for ROP, B always, C for ILP); it does **not** paraphrase compliance language.
- `{{…}}` slots inside C are filled from collected facts (surrender-charge year, premium-holiday year) — never invented.
- If Melody's approved wording differs from this starter library, **her wording wins** — this file is replaced with her confirmed text before live use.

