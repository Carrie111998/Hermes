# MTU BOR Assistant — Synthetic E2E Test Transcript

**Date:** 2026-07-13 · **Bot:** @pcl_mtu_bor_bot · **Runtime:** hermes-mtu (Studio-local pilot, HERMES_HOME=~/.hermes-mtu) · **Model:** gpt-5.4-mini (provider `custom`, OpenAI-direct) · **Stack:** PRODUCTION (Hermes gateway + native Telegram ingress).

**Synthetic only — no real client PII.** Tester account: 276672685 (allowlisted). Two fabricated cases; no Melody/Finexis real data.

## What this proves (DoD 1/2/3/5)
- **DoD 1** — a real Telegram message to the live bot returns a real agent response (not a stub/echo).
- **DoD 2** — full BOR flow: agent classifies the replacement path (Term→Term) → runs the required checks → asks for missing fields → drafts a copy-pasteable BOR from the template, inserting the standard disclosures and leaving `[[MISSING: ECIM]]` rather than inventing a reference number.
- **DoD 3 (config-driving-behaviour)** — Case A (ROP) fires the ROP-specific checks (replacement-disadvantages-acknowledged + alternatives-to-replacement: increase SA / add riders / convert) and inserts the 4-point ROP disadvantages disclosure. Case B (non-ROP new purchase) does NOT fire those ROP checks. This is the constitution/knowledge gating behaviour, not generic-LLM output.
- **DoD 5** — READY-state controlled-tester pass, run through the production stack, synthetic case only.

Note: neither case triggered ILP checks (CKA/fund/risk) — correct, both are non-ILP term cases. An ILP case would additionally fire those per bor-required-checks.yaml `applies_when: ilp`.

---

## Transcript (chronological)

### ADVISOR (synthetic tester 276672685)  ·  2026-07-13 20:57:18 +08

hi

---
### MTU ASSISTANT (@pcl_mtu_bor_bot)  ·  2026-07-13 20:57:19 +08

📬 No home channel is set for Telegram. A home channel is where Hermes delivers cron job results and cross-platform messages.

Type /sethome to make this chat your home channel, or ignore to skip.

---
### MTU ASSISTANT (@pcl_mtu_bor_bot)  ·  2026-07-13 20:57:25 +08

⚠️ Provider authentication failed: Unknown provider 'openai-direct-primary'. Check 'hermes model' for available providers, or run 'hermes doctor' to diagnose config issues.

---
### ADVISOR (synthetic tester 276672685)  ·  2026-07-13 20:57:39 +08

I have an MTU BOR case. Existing plan: client has Prudential PruTerm (Term Life), sum assured 300k for death/TPD, premium 1200/yr, coverage till age 65. Proposed: Singlife EliteTerm (Term Life), SA 500k death/TPD/CI, premium first year 1500, subsequent 1500, term to age 70. This IS a replacement of policy — replacing the PruTerm with EliteTerm. Draft me the BOR.

---
### MTU ASSISTANT (@pcl_mtu_bor_bot)  ·  2026-07-13 20:57:45 +08

⚠️ Provider authentication failed: Unknown provider 'openai-direct-primary'. Check 'hermes model' for available providers, or run 'hermes doctor' to diagnose config issues.

---
### ADVISOR (synthetic tester 276672685)  ·  2026-07-13 20:59:53 +08

I have an MTU BOR case. Existing plan: Prudential PruTerm (Term Life), sum assured 300k death/TPD, premium 1200/yr, coverage till age 65. Proposed: Singlife EliteTerm (Term Life), SA 500k death/TPD/CI, premium first year 1500, subsequent 1500, term to age 70. This IS a replacement of policy — replacing PruTerm with EliteTerm. Help me draft the BOR.

---
### MTU ASSISTANT (@pcl_mtu_bor_bot)  ·  2026-07-13 21:00:07 +08

I can draft it, but this case is not ready yet for a compliant BOR because a few required checks are still missing.

You’ve given me the anchors:
- Existing: Prudential PruTerm, term life, SA 300k death/TPD, premium 1200/yr, to age 65
- Proposed: Singlife EliteTerm, term life, SA 500k death/TPD/CI, premium 1500/yr, to age 70
- Path: Term → Term replacement

I still need these before I draft:

1. Client’s reason for change  
   - In the client’s own words, why are they switching?

2. Alternatives considered  
   - What else was discussed, and why was EliteTerm preferred?
   - For ROP, did you also explore:
     - increasing SA under PruTerm
     - adding riders
     - converting the existing policy?

3. Premium sustainability check  
   - What is the client’s income or surplus?
   - I need this to check whether the new premium is more than 50% of income/surplus.

4. Replacement disadvantages acknowledged  
   - Was the client advised of the switching disadvantages and still wishes to proceed?

5. Product comparison freshness  
   - Are the comparison products/current market options still up to date?

6. Reference number  
   - Do you need an ECIM / application / reference number in the BOR?
   - If yes, what is it?

If you send me those 6 items, I’ll turn it into a copy-pasteable BOR draft immediately.

---
### ADVISOR (synthetic tester 276672685)  ·  2026-07-13 21:00:31 +08

Here are the 6 items: 1) Reason for change: client says PruTerm premium is rising and wants CI cover which PruTerm lacks. 2) Alternatives: discussed keeping PruTerm and adding a CI rider, and increasing PruTerm SA — client preferred a clean switch to EliteTerm for the bundled CI and longer term to 70. Converting the policy was explored but not available. 3) Client income 8000/month, surplus about 2000/month; new premium 1500/yr is well under 50%. 4) Yes, client was advised of the replacement disadvantages and wishes to proceed. 5) Comparison list is current as of this month. 6) ECIM reference: leave a placeholder, advisor will fill. Now draft the BOR.

---
### MTU ASSISTANT (@pcl_mtu_bor_bot)  ·  2026-07-13 21:00:42 +08

Absolutely — here’s a copy-pasteable BOR draft for advisor review.

BOR Draft

To address the client’s protection needs for Death, TPD and Critical Illness, the client currently has 1 plan with Prudential: PruTerm (Term Life), with a sum assured of S$300,000 for Death and TPD, premium of S$1,200 p.a., and coverage till age 65.

After a recent review, the client shared that the PruTerm premium is rising and that the existing plan does not provide Critical Illness cover, which the client would like to have. As a result, the client wishes to switch to a plan that provides broader protection and a longer coverage period.

After discussing the pros and cons of keeping the existing PruTerm and adding a CI rider, as well as increasing the sum assured under the existing policy, the client preferred Singlife EliteTerm (Term Life) due to its bundled CI coverage and longer coverage term to age 70. A policy conversion was also explored, but it was not available.

Client has chosen Singlife EliteTerm (Term Life), with sum assured of S$500,000 for Death, TPD and CI, premium of S$1,500 for first year and S$1,500 for subsequent years, and coverage till age 70.

The recommended plan provides:
- Higher Death/TPD coverage: S$500,000 vs S$300,000
- Critical Illness coverage: added under the proposed plan, whereas the existing plan does not provide CI cover
- Longer coverage duration: till age 70 vs age 65

The new premium of S$1,500 p.a. is higher than the existing premium of S$1,200 p.a.; however, this is justified by the improved protection level, added CI coverage, and longer coverage term. Based on the client’s monthly income of S$8,000 and surplus of about S$2,000, the proposed premium is well within affordability and does not exceed the 50% sustainability threshold.

The client was advised of the disadvantages of replacement of policy, including possible charges/penalties from terminating the existing plan, possible loss of benefits accumulated under the existing policy, the possibility of receiving lower or less suitable benefits at a higher or similar cost, and the risk of losing coverage for any existing medical conditions. The client understood these points and wishes to proceed with the replacement.

The comparison list used is current as of this month.

Reference / ECIM: [[MISSING: advisor to insert ECIM reference number]]

Standard disclosures to include

The client was informed that in the event of non-disclosure of any pre-existing medical conditions, the insurer has the right to not pay out benefits as stated if diagnosed due to pre-existing conditions.

The product was recommended after fact-find, needs analysis, and product comparison.

The client is aware that the Financial Consultant may receive additional commission for selling the recommended product.

The client has agreed for soft copies of the documents to be electronically mailed after the company has processed them.

Coverage note
Used:
- Existing plan: Prudential PruTerm, Term Life, S$300,000 Death/TPD, S$1,200 p.a., till age 65
- Proposed plan: Singlife EliteTerm, Term Life, S$500,000 Death/TPD/CI, S$1,500 first year / subsequent, till age 70
- ROP status: yes, Term → Term
- Client rationale: premium rising + wants CI cover
- Alternatives discussed: add CI rider, increase SA, policy conversion explored
- Sustainability: income S$8,000/month, surplus about S$2,000/month, premium within threshold
- ROP acknowledgment: yes
- Freshness: current this month
- Reference number: placeholder left for advisor

Still missing:
- Actual ECIM / reference number

If you want, I can also turn this into a shorter WhatsApp-style version or make it sound more like a typical YFB recommendation paragraph used in the unit.

---
### ADVISOR (synthetic tester 276672685)  ·  2026-07-13 21:01:07 +08

New separate case, different client. This is NOT a replacement — the client has no existing policy being replaced. Client is a fresh buyer with no current coverage. Proposed: Singlife EliteTerm (Term Life), SA 400k death/TPD/CI, premium 1300/yr, term to age 65. Reason: first-time buyer wants basic protection. Income 6000/month. This is a brand-new purchase, not an ROP. Draft the BOR.

---
### MTU ASSISTANT (@pcl_mtu_bor_bot)  ·  2026-07-13 21:01:13 +08

🐍 execute_code: "income_monthly=6000 premium_annual=13..."

---
### MTU ASSISTANT (@pcl_mtu_bor_bot)  ·  2026-07-13 21:01:22 +08

Got it — this is a new purchase, not an ROP. I can draft it, but I’m still missing a few BOR checklist items I need before I write the final version.

What I have already:
- Existing position: no current policy being replaced
- Proposed: Singlife EliteTerm, Term Life, SA S$400k death/TPD/CI, premium S$1,300/yr, term to age 65
- Reason: first-time buyer wants basic protection
- Income: S$6,000/month
- Sustainability: premium is about 1.81% of annual income, so this is comfortably within the 50% threshold

Still needed before I draft:
1. Alternatives considered
   - What other products/methods were discussed, and why was EliteTerm preferred?

2. Coverage comparison
   - Since there’s no existing policy, confirm this is simply “no existing coverage” rather than any employer/group coverage or other protection already in place.

3. Product comparison freshness
   - Are the comparison products/current market options still current?

4. Reference number
   - Do you need an ECIM / application / reference number?
   - If yes, give it; otherwise I’ll leave a placeholder.

Once you send those, I’ll draft the BOR immediately.


