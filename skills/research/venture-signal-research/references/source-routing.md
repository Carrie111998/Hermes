# Source routing and content health

Plan coverage before searching. A source may serve more than one lane, but
syndicated copies count as one underlying source.

| Source lane | Use it for | Preferred evidence | Common failure |
|---|---|---|---|
| Primary | What a buyer, vendor, regulator, or dataset directly states | Official documents, filings, pricing, first-party posts, raw datasets | Marketing copy presented as independent proof |
| Independent | Corroboration and market context | Reputable reporting, analyst work, academic or industry research | Repeating a primary claim without new verification |
| Community | Buyer language, workflows, and pain hypotheses | Public discussions, reviews, issue trackers, public forums | Login walls, sampling bias, unverifiable identities |

Browser use is a retrieval method, not a source lane. A primary page opened in
a browser remains `primary`; the coverage log records the fallback method.

## Routing sequence

1. Use `web_search` to find candidate URLs in the planned source lanes.
2. Make the preferred attempt with `web_extract` before using a target for a
   material claim.
3. Retry that preferred route once only when the failure is plausibly transient.
4. Use one fallback total: either open the same public target once with
   `browser_navigate` when rendering is the problem, or try one suitable
   configured read-only route/source that answers the same evidence question.
   Do not do both.
5. If the fallback lacks substantive content, record a coverage gap and
   continue. Do not install software, authenticate, reuse cookies, evade access
   controls, change proxies, post, or contact people without explicit approval.

## Content-health gate

A retrieval is healthy only when its body contains enough relevant substance
to support the proposed evidence. Tool success, an HTTP status, a page title,
or a non-empty response is not enough.

Reject or downgrade results that are login/paywall prompts, anti-bot pages,
cookie-only screens, navigation shells, empty result sets, generic errors, or
content unrelated to the decision question. Record the attempted URL, observed
failure, preferred route, transient retry (if any), one fallback total, and the
resulting gap so the coverage report is auditable.

## Stopping rules

Stop when the predeclared time/source budget is exhausted, all material lanes
are covered or explicitly marked as gaps, and additional sources only repeat
existing evidence. A blocked lane lowers confidence; it does not justify an
unbounded workaround.
