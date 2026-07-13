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
