# MTU P0 — BOR Draft Template (recommendation-rationale narrative)

This is the concise narrative structure used per recommended product / need-bucket. It is grounded in files 01 and 17 and confirmed across the ten text-readable OneDrive cases. Variable case facts are never invented. Corpus-standard alternatives wording is selected by product category rather than collected from the advisor each time.

## Interaction before drafting

- Use every fact already supplied. Do not repeat it back as an intake recap.
- Derive coverage movement, premium movement, and the 50% sustainability result from the figures provided.
- Insert the category-specific alternatives sentence below. Do not ask which alternatives were considered unless the path is novel, the advisor names an exception, or the standard wording conflicts with the case.
- Do not ask about comparison-list freshness or reference numbers by default. Reference numbers are outside the BOR narrative unless the advisor explicitly requests one or names a workflow that requires one.
- If irreducible facts are missing, ask for all of them once in one compact message. Irreducible facts are: material plan figures needed for a truthful comparison; the client's stated rationale; ROP acknowledgement and replacement-options confirmation; and ILP-only suitability facts when an ILP is involved.
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

4. Recommended product and material figures

> The client has chosen {{PROPOSED_PLAN_NAME_AND_RIDERS}}. The plan provides {{SA_BY_BENEFIT}}, costs {{PREMIUM_FIRST_YEAR_AND_SUBSEQUENT}}, and covers the client {{POLICY_TERM_OR_TO_AGE}}. {{ILP_ONLY: Minimum Investment Period {{MIP}}.}}

Do not claim the plan matches affordability unless the supplied figures support that conclusion.

5. Derived comparison

> {{COVERAGE_DELTA_STATEMENT}} {{PREMIUM_MOVEMENT_STATEMENT}} {{SUSTAINABILITY_STATEMENT_IF_DERIVABLE}}

State the before/after amounts and durations directly. If the premium is higher, use only supplied product differences or the client's stated reason as justification. If income/surplus was not supplied, omit the sustainability conclusion or mark it unavailable; do not guess.

6. Conditional blocks

- ROP only: insert replacement disclosures from standard-disclosures.md only after the advisor confirms the client was advised, understood the disadvantages, wishes to proceed, and the standard replacement options were explored or not applicable.
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

## Hard rules

- Do not invent SA, premiums, reference numbers, client rationale, ROP acknowledgement, CKA/risk profile, fund alignment, or product-specific disclosure values.
- Do not make the product recommendation. The advisor chooses the product; the agent drafts the justification.
- The draft is for advisor review, not compliance sign-off. Escalate sensitive or unclear cases to Melody.
