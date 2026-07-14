# MTU BOR Assistant — Knowledge Base

This is the operating knowledge for the MTU (Melody Tan Unit / Finexis) BOR-generation assistant.
It is loaded into the system prompt (hermes reads $HERMES_HOME/SOUL.md). The constitution
(mtu_constitution.yaml) supplies identity + the bor_generation job instructions; this file supplies
the BOR checks table, replacement-path taxonomy, draft template, and the approved disclosures library.

---

---

## bor-required-checks.yaml

# MTU P0 — BOR Required Checks ("the BOR table")
#
# The checklist the MTU agent runs before drafting a BOR. Grounded in Melody's
# 18 real cases + the amelia-finexis bor-scraper encoded standard (see GROUNDING.md).
# HELD for Amelia; Melody confirms/edits this table before the agent drafts live.
#
# resolution values:
#   collect          - an irreducible case fact; ask only when missing
#   derive           - compute from supplied case facts; never ask for the conclusion
#   template_default - insert the corpus-grounded category template; ask only for an explicit exception or novel path
#   operational      - maintained outside the case; never ask the advisor per case
#
# applies_when values:
#   always     - every BOR
#   rop        - only when it is a replacement of policy (switch)
#   ilp        - only when an investment-linked / investment product is involved
#   shield     - only for Integrated Shield Plan / hospitalisation cases
#   accident   - only for personal-accident product recommendations
#
# collect: what the agent must obtain from the advisor when needed for a truthful draft
# derive:  what the agent computes / states from supplied inputs
# template_default: corpus-grounded standard wording selected by product category
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
    resolution: collect
    ask_if_missing: "Existing plan(s): supply the facts required by the matching category contract below. Say 'none' for a first purchase."
    collect: [fields_from_matching_category_contract]
    why: "The BOR must refer to the client's starting position clearly."
    applies_when: always
  - id: proposed_plan
    resolution: collect
    ask_if_missing: "Proposed plan: supply the facts required by the matching category contract below."
    collect: [fields_from_matching_category_contract]
    why: "The recommended product with its specifics anchors the whole BOR."
    applies_when: always
  - id: is_replacement
    resolution: collect
    ask_if_missing: "ROP or new purchase? If ROP, identify each replaced and replacing product."
    collect: [is_rop, replaced_products, replacing_products, replaced_type_investment_or_insurance]
    why: "ROP cases need the extra replacement checks + more careful wording."
    applies_when: always

# Category is a field contract, not a prose label. Do not apply one category's
# required fields to another category.
field_contracts:
  protection_life:
    categories: [term_life, whole_life, investment_linked_protection, critical_illness]
    collect_existing: [insurer, plan_name, category, sum_assured_by_benefit, premium, coverage_till_age]
    collect_proposed: [insurer, plan_name, riders, category, sum_assured_by_benefit, premium_first_year, premium_subsequent, policy_term]
    ilp_additional_collect: [min_investment_period]
    comparison: "State supplied before/after sums assured and coverage durations."
  shield_hospitalisation:
    categories: [integrated_shield_plan, shield, hospitalisation]
    collect_existing: [insurer, exact_base_plan_name, exact_versioned_rider_name, material_premium]
    collect_proposed: [insurer, exact_base_plan_name, exact_versioned_rider_name, material_premium]
    collect_for_rop: [is_rop, replaced_products, replacing_products]
    collect_rationale: [client_stated_rationale]
    derive: "A qualitative before/after statement using the exact supplied base-plan and rider names plus the supplied client rationale."
    rider_name_rule: "The exact/versioned rider name identifies the arrangement only. It never establishes or licenses a numeric benefit claim."
    missing_rider_rule: "If the rider is missing or ambiguous, ask only for the exact/versioned rider name."
    never_collect: [rider_benefit_limits, deductible, coinsurance]
    never_print: [rider_benefit_limits, deductible, coinsurance]
    supplied_exception_rule: "If the advisor explicitly supplies an exceptional or nonstandard fact that is material to the rationale, preserve it accurately in case context; never solicit it and never print the prohibited value in the BOR."
    excluded_categories: [health_medical, careshield, eldershield, personal_accident, accident, accident_and_health]
  other_or_unknown:
    resolution: operational_escalation
    rule: "Do not force protection/life or Shield fields onto a different category. If the category itself is unclear, ask one short category clarification. If the category is known but has no approved field contract here—including CareShield/ElderShield, accident, or other A&H—do not draft or invent its required fields; escalate to Melody for the category contract."

# ── Required checks (the compliant-BOR must-covers) ───────────────────────────
checks:
  - id: coverage_comparison
    resolution: derive
    select_by_category:
      protection_life:
        categories: [term_life, whole_life, investment_linked_protection, critical_illness]
        inputs: [existing_plan.sum_assured_by_benefit, existing_plan.coverage_till_age, proposed_plan.sum_assured_by_benefit, proposed_plan.coverage_till_age]
        derive: "State the supplied before/after sums assured and coverage durations; shortfalls are stated, not hidden."
      shield_hospitalisation:
        categories: [integrated_shield_plan, shield, hospitalisation]
        inputs: [existing_plan.exact_base_plan_name, existing_plan.exact_versioned_rider_name, proposed_plan.exact_base_plan_name, proposed_plan.exact_versioned_rider_name, client_stated_rationale]
        derive: "State the qualitative before/after arrangement using exact plan and rider names plus the supplied rationale. Do not infer or state numeric benefits, deductible, or coinsurance."
    why: "The comparison must match the product category instead of importing life-plan fields into Shield cases."
    grounded_by_category:
      protection_life: "files 01, 17 — supplied sums assured and coverage durations drive the comparison"
      shield_hospitalisation: "preserved build grounding records Shield→Shield examples in files 01 and 16; Amelia approved the exact-name qualitative contract on 2026-07-14"
    applies_when: always
  - id: premium_comparison
    resolution: derive
    select_by_category:
      protection_life:
        inputs: [existing_plan.premium, proposed_plan.premium_first_year, proposed_plan.premium_subsequent]
        derive: premium_movement_statement
      shield_hospitalisation:
        inputs: [existing_plan.material_premium, proposed_plan.material_premium]
        derive: "State the movement between the supplied material premiums without inferring an unprovided premium component."
    collect_if_needed: "client-stated reason for a higher premium when the supplied category-appropriate facts do not explain it"
    why: "The recommendation must explain the trade-off clearly."
    grounded: "files 01, 17 — 'premiums are lowered compared to previous planning' / justification when higher"
    applies_when: always
  - id: income_sustainability_threshold
    resolution: collect
    ask_if_missing: "Do the client's total annual premiums exceed 50% of annual income? Answer yes or no."
    collect: [total_annual_premiums_above_50_percent_of_annual_income]
    never_collect: [income, annual_income, monthly_income, surplus, net_worth]
    output:
      no: "The client's total annual premiums do not exceed 50% of annual income."
      yes: "The client's total annual premiums exceed 50% of annual income, and the client was advised to consider the sustainability of the premium commitment."
    exact_mapping_rule: "Copy the sentence matching the supplied boolean exactly. YES maps only to the `yes` sentence; NO maps only to the `no` sentence. Never invert, recalculate, infer, or soften the answer."
    why: "Key affordability check before finalising; the advisor supplies the threshold result without exposing the client's financial figures."
    grounded: "Amelia correction 2026-07-14; 50% sustainability language appears in files 03, 13, 17"
    applies_when: always
  - id: alternatives_considered
    resolution: template_default
    select_by_recommended_category:
      term_whole_life_or_ilp_protection:
        categories: [term_life, whole_life, investment_linked_protection]
        standard_alternatives: [whole_life, term_life, investment_linked_plan]
        sentence: "After discussing the pros and cons of whole life, term life and investment-linked plans, the client preferred {{RECOMMENDED_CATEGORY}} because {{CLIENT_STATED_RATIONALE_OR_SUPPLIED_PRODUCT_FIT}}."
      ilp_or_wealth:
        categories: [investment_linked_plan, investment_platform, endowment, retirement_annuity, indexed_universal_life]
        standard_alternatives: [endowment, investment, annuity]
        sentence: "After discussing the pros and cons of endowment, investment and annuity solutions, the client preferred {{RECOMMENDED_CATEGORY}} because {{CLIENT_STATED_RATIONALE_OR_SUPPLIED_PRODUCT_FIT}}."
      shield:
        categories: [integrated_shield_plan, shield, hospitalisation]
        standard_alternatives: [other_insurers_shield_plans]
        sentence: "After comparing Shield plans from other insurers, the client preferred {{RECOMMENDED_PLAN}} because {{CLIENT_STATED_RATIONALE_OR_SUPPLIED_PRODUCT_FIT}}."
    per_case_question: false
    exception_rule: "Do not ask for alternatives by default. Ask only when the path is novel, the advisor states different alternatives, or the standard sentence would conflict with supplied facts."
    why: "The BOR must explain why the recommended product was chosen over alternatives."
    grounded: "files 01, 17 — 'After discussing the pros and cons of [WL/term/ILP/endowment/…], the client preferred X because…'"
    applies_when: always
  - id: reason_for_change
    resolution: collect
    ask_if_missing: "Client's reason for the change/recommendation, in the client's own terms."
    collect: [client_stated_rationale]
    why: "The draft must reflect the client's ACTUAL rationale — the agent must not invent one."
    grounded: "every readable ROP case — 'the reason for the switch/replacement is highlighted below:'"
    never: "invent a rationale the advisor did not state"
    applies_when: always
  - id: rop_disadvantages_acknowledged
    resolution: template_default
    per_case_question: false
    sentence: "The client was advised of the replacement disadvantages and wishes to proceed."
    contradiction_rule: "If the advisor explicitly states that the client was not advised, does not wish to proceed, or is uncertain, suppress the standard sentence and escalate to Melody. Do not ask routinely."
    contradiction_output_rule: "Return only a concise Melody-escalation note. Do not produce a holding draft, partial BOR, disclosures, or placeholders."
    why: "ROP compliance — client must be shown the downside of switching before proceeding."
    grounded: "Amelia-approved standard template 2026-07-14; files 01, 17 — the 4 general disadvantages + 'wish to proceed notwithstanding'"
    applies_when: rop
  - id: alternatives_to_replacement_explored
    resolution: template_default
    per_case_question: false
    sentence: "Other available options—such as increasing the sum assured under the existing policy, attaching riders, or converting the policy—have been explored. The recommendation to proceed with a replacement was made only after a thorough assessment confirmed that it is suitable and in the best interest of the client."
    why: "ROP declaration requires confirming replacement was chosen only after these were considered and it is best-interest."
    grounded: "file 01 — 'Other available options—such as increasing the sum assured, attaching riders, or converting the policy—have been explored.'"
    applies_when: rop
  - id: product_comparison_freshness
    resolution: operational
    per_case_question: false
    derive: "Use the maintained approved comparison knowledge; escalate only if the named product/path is absent or visibly stale."
    why: "Comparisons must not be against outdated products. Melody flags a ~monthly refresh of the comparison list."
    grounded: "spec + Melody note"
    applies_when: always
  - id: reference_numbers
    resolution: collect_on_explicit_request
    per_case_question: false
    collect: [reference_numbers_if_requested_or_required_by_named_workflow]
    why: "Some BOR flows require reference numbers."
    never: "invent a reference number — ask the advisor, or leave a clearly-marked placeholder"
    applies_when: always
  # ── ILP-specific checks (only when an investment-linked product is involved) ──
  - id: ilp_cka_risk_profile
    resolution: collect
    ask_if_missing: "ILP only: CKA result and client risk profile."
    collect: [cka_result, risk_profile]
    why: "ILP recommendations must reference CKA + risk profile for fund suitability."
    grounded: "file 01 — 'Client did not pass CKA and has a risk profile of aggressive.'"
    applies_when: ilp
  - id: ilp_fund_justification
    resolution: collect
    ask_if_missing: "ILP only: fund name, objective, and the client's goal/horizon it aligns with."
    collect: [fund_name, fund_objective, client_goal_or_horizon]
    why: "Fund choice must be justified against the client's investment horizon and goals."
    grounded: "file 01 — fund recommended as objective aligns with long-term horizon"
    applies_when: ilp
  - id: ilp_risk_disclosures
    resolution: derive
    inputs: [product_summary.surrender_charge_timeline, product_summary.premium_holiday_year, product_summary.bonus_terms]
    derive: ilp_disclosure_block   # see standard-disclosures.md
    why: "ILP-specific caveats are mandatory in the BOR narrative."
    grounded: "files 01, 03, 13, 18"
    applies_when: ilp

# ── Output contract ───────────────────────────────────────────────────────────
output:
  produce:
    - "a concise Telegram-ready BOR draft (the recommendation-rationale narrative), per recommended product / need-bucket"
    - "only when needed, one short note listing unresolved placeholders or anything to check with Melody"
  interaction:
    - "If irreducible COLLECT facts are missing, ask once in one compact message instead of drafting; do not recap facts already supplied, output a partial BOR, or append an unresolved list."
    - "Do not ask for derived conclusions, standard alternatives, product-comparison freshness, or reference numbers by default."
    - "For Shield/hospitalisation, ask for an exact/versioned rider name when missing or ambiguous; never ask for or print rider benefit limits, deductible, or coinsurance."
    - "For ROP, insert the standard replacement-disadvantages sentence without asking; explicit contradiction or uncertainty suppresses it and escalates to Melody."
    - "A known product category with no approved field contract is a Melody escalation, not permission to draft from generic fields."
    - "For affordability, ask only the 50%-of-income yes/no question. Never request or print the client's income or surplus figures."
    - "Preserve the affordability boolean exactly: YES = exceeds threshold and caution; NO = does not exceed threshold."
    - "Use plain text only: no Markdown asterisks, decorative headings, tables, or repeated offers to reformat."
    - "FINAL RESPONSE CHECK: before sending, remove every Markdown marker (`*`, `#`, `---`), any intake recap, and any closing offer such as 'if you want'. If any remain, rewrite once in concise plain text."
  never:
    - "make product recommendations by itself"
    - "invent missing facts (SA, premium, reference numbers, client rationale)"
    - "invent a missing material case fact; ask once for irreducible facts or leave an explicit placeholder when the draft can still be useful"
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

This is the concise narrative structure used per recommended product / need-bucket. It is grounded in files 01 and 17 and confirmed across the ten text-readable OneDrive cases. Variable case facts are never invented. Corpus-standard alternatives wording is selected by product category rather than collected from the advisor each time.

## Interaction before drafting

- Use every fact already supplied. Do not repeat it back as an intake recap.
- Derive coverage and premium movement from the category-appropriate facts provided. For sustainability, use only the advisor's yes/no answer to whether total annual premiums exceed 50% of annual income; never request or print income or surplus figures.
- Insert the category-specific alternatives sentence below. Do not ask which alternatives were considered unless the path is novel, the advisor names an exception, or the standard wording conflicts with the case.
- Match intake and comparison fields to the product category. For Shield/hospitalisation, collect exact base-plan and exact/versioned rider names, material premium, ROP mapping/status, and the client's rationale. Never ask for or print rider benefit limits, deductible, or coinsurance. If a rider is missing or ambiguous, ask only for its exact/versioned name.
- If a known category has no approved field contract in bor-required-checks.yaml—including CareShield/ElderShield, accident, or other A&H—do not draft from generic assumptions. Escalate to Melody for that category contract.
- Do not ask about comparison-list freshness or reference numbers by default. Reference numbers are outside the BOR narrative unless the advisor explicitly requests one or names a workflow that requires one.
- If irreducible facts are missing, ask for all of them once in one compact message instead of drafting. Irreducible facts are: category-appropriate material plan facts needed for a truthful comparison; the client's stated rationale; old→new mapping and ROP status; and ILP-only suitability facts when an ILP is involved. ROP acknowledgement wording is a template default, not an intake question. Do not output a partial BOR, placeholders, or an unresolved list in that turn.
- If the missing fact is non-blocking, draft with a clear [[MISSING: ...]] placeholder instead of conducting another question round.

## Category-specific standard alternatives sentences

Protection path — recommended Term, Whole Life, or protection ILP:

> After discussing the pros and cons of whole life, term life and investment-linked plans, the client preferred {{RECOMMENDED_CATEGORY}} because {{CLIENT_STATED_RATIONALE_OR_SUPPLIED_PRODUCT_FIT}}.

ILP / wealth path — recommended ILP, investment platform, endowment, annuity, or IUL:

> After discussing the pros and cons of endowment, investment and annuity solutions, the client preferred {{RECOMMENDED_CATEGORY}} because {{CLIENT_STATED_RATIONALE_OR_SUPPLIED_PRODUCT_FIT}}.

Shield path — recommended Integrated Shield Plan / medical plan:

> After comparing Shield plans from other insurers, the client preferred {{RECOMMENDED_PLAN}} because {{CLIENT_STATED_RATIONALE_OR_SUPPLIED_PRODUCT_FIT}}.

The reason slot may use only the client's stated rationale or an objective product fit directly supported by supplied figures/features. Do not invent alternatives outside the approved template, a product preference not supplied by the advisor, or a rationale. For an unknown category, ask one short clarification instead of forcing a template.

## Narrative order per recommended product

1. Need and existing position

> To address the client's {{NEED}}, the client currently has {{EXISTING_PLANS_OR_NO_EXISTING_COVER}}.

2. Client's reason

> After a recent review, the client stated {{CLIENT_STATED_RATIONALE}}.

3. Standard alternatives sentence

Insert exactly one category-specific sentence from the section above.

4. Recommended product and material facts

Protection / life path:

> The client has chosen {{PROPOSED_PLAN_NAME_AND_RIDERS}}. The plan provides {{SA_BY_BENEFIT}}, costs {{PREMIUM_FIRST_YEAR_AND_SUBSEQUENT}}, and covers the client {{POLICY_TERM_OR_TO_AGE}}. {{ILP_ONLY: Minimum Investment Period {{MIP}}.}}

Shield / hospitalisation path:

> The client has chosen {{EXACT_PROPOSED_BASE_PLAN_NAME}} with {{EXACT_VERSIONED_RIDER_NAME}}, at a material premium of {{MATERIAL_PREMIUM}}.

For Shield, the plan and rider names identify the arrangement, not numeric benefits. Do not ask for or print rider benefit limits, deductible, or coinsurance. If an advisor volunteers an exceptional or nonstandard fact material to the rationale, preserve it accurately in case context without printing the prohibited value in the BOR.

Do not claim the plan matches affordability unless the supplied figures support that conclusion.

5. Derived comparison

> {{COVERAGE_DELTA_STATEMENT}} {{PREMIUM_MOVEMENT_STATEMENT}} {{SUSTAINABILITY_STATEMENT_FROM_50_PERCENT_BOOLEAN}}

For protection/life, state supplied before/after sums assured and durations directly. For Shield/hospitalisation, state a qualitative before/after arrangement using the exact existing and proposed plan/rider names plus the supplied rationale; do not infer numeric benefits. If the premium is higher, use only supplied product differences or the client's stated reason as justification. Copy the exact sentence for the supplied boolean: NO → "The client's total annual premiums do not exceed 50% of annual income." YES → "The client's total annual premiums exceed 50% of annual income, and the client was advised to consider the sustainability of the premium commitment." Never invert, recalculate, infer, or soften this mapping. Never request or expose the client's income or surplus.

6. Conditional blocks

- ROP only: insert "The client was advised of the replacement disadvantages and wishes to proceed." as standard template text without asking the advisor to reconfirm it. If the advisor explicitly contradicts this or expresses uncertainty, suppress it and return only a concise Melody-escalation note—no holding draft, partial BOR, disclosures, or placeholders. The replacement-options declaration is also standard template text; insert it without asking.
- Non-ROP: exclude every ROP acknowledgement and replacement disclosure.
- ILP only: include CKA, risk profile, fund/objective alignment, and the ILP disclosure block. Fill product-specific disclosure values from supplied product documents only.
- Non-ILP: exclude every ILP-specific question and disclosure.

7. General disclosures

Insert the approved general disclosure block from standard-disclosures.md without paraphrasing it.

## Output contract

- Return the BOR draft directly. Keep it copy-pasteable, short, and in plain text.
- Add a short "Check before use:" note only for unresolved placeholders or a Melody escalation. Do not list every field used.
- Do not add a reference-number placeholder unless the advisor requested it or the named workflow requires it.
- Do not end with an offer to shorten, reformat, or produce another version.
- Do not emit Markdown asterisks anywhere in the Telegram response. Use plain headings and hyphens only when needed.
- Before sending, remove every Markdown marker (`*`, `#`, `---`), intake recap, and closing offer such as "if you want". If any remain, rewrite once in concise plain text.

## Hard rules

- Do not invent SA, premiums, reference numbers, client rationale, CKA/risk profile, fund alignment, or product-specific disclosure values. The approved ROP acknowledgement and alternatives declarations are template defaults; suppress them on explicit contradiction or uncertainty.
- Do not make the product recommendation. The advisor chooses the product; the agent drafts the justification.
- The draft is for advisor review, not compliance sign-off. Escalate sensitive or unclear cases to Melody.

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
