# lead research

lead research is the most vital feature of the current agent structure. it
should work effectively and provide pinpoint leads to the users depending on the
market and the scoring system the user determined.

**who this is for.** a sales representative selling wholesale or for export, in
any sector. the sale is always the same shape — we sell in volume to a business
that resells, distributes, or buys for its own operations, usually in another
country — and the sector is whatever the customer happens to sell: machinery,
textiles, food, chemicals, furniture, building materials, packaging, anything.
the motion is fixed. the sector is not, and nothing in this product may assume
one.

that distinction decides what generalises. the workflow, the criteria, the
verdicts and the contact rules are the same for every sector, because every
wholesale sale asks the same questions: does this company buy what i make, do
they buy at my scale, are they in my market, are they buying now, and can i
reach whoever decides. what changes per sector is which *facts* answer those
questions — store count tells a furniture exporter a great deal and a chemicals
exporter nothing — and that belongs in a sector playbook, never in the engine.

everything below is written so that a lead's score means the same thing on
monday as it does on friday, and so that we never show a number we cannot
explain to the customer who paid for it.

## the workflow

1. the user selects the markets they want to sell into — one country or thirty —
   and says what they sell: a sector, hs codes, or named products. any one of the
   three is enough to start, and plain product names have to work on their own,
   because plenty of sellers know their product and not its customs code.
2. the user determines the scoring system. we supply the criteria — the list is
   ours, because every criterion has to be something we can actually go and
   find — and the user decides how important each one is to them. weights total
   100, set in the webui per campaign, and a campaign can be cloned so a user can
   keep several scoring systems side by side. the criteria are the same in every
   sector; what a sector changes is which facts can satisfy them.
3. the system finds candidate companies from the sources configured for that
   campaign.
4. each candidate is checked for eligibility before anything is scored. an
   ineligible candidate is dropped with a named reason, not scored badly.
5. eligible candidates are researched, scored against the user's weights, and
   shown with the score and the evidence behind it.
6. for a company the user likes, they can research the contacts.

## the customer profile

every customer we take on has a profile: who they are, the product range they
sell, and labels describing where they fit. it is the input to everything below —
which terms we search, which sector playbook applies, which companies we spend
money researching, and which leads they see at all.

**the profile is built from validated sources, then confirmed in passing.** the
company's own website, its filings, credible press: agentic research assembles a
draft from those, and because they are official or authoritative the facts in it
are validated like any other. what research cannot read is emphasis — which
products carry their margin, which markets they are pushing this year, which
lines they are winding down. nobody publishes that. so we ask, in place, with a
small prompt at the moment it matters: "we see ovens, hobs and hoods — which do
you most want leads for?" cheap to answer, and it is the difference between a
range that is accurate and one that is useful.

the profile is versioned. a score computed under last quarter's range has to
stay explainable next quarter.

**labels are ours, and customers do not see them.** they are how the system
decides what to show; they are not a feature we are selling, and exposing the
machinery gives away the reason to pay for it. two things follow, and both are
requirements rather than preferences:

- **admin sees every label**, who set it and when. when a customer complains
  about lead quality, the answer is usually a label, and it has to be findable.
- **outcomes are how a wrong label surfaces.** a customer who cannot see a label
  cannot tell us it is wrong. so conversion measured per label is not a
  reporting nicety, it is the only error channel we have. this is why the
  outcome loop is load-bearing rather than optional.

hiding labels is not hiding reasons. the evidence behind a lead — this company
distributes built-in ovens, here is the page that says so, here is when we read
it — is the product, and it is exactly what makes the list worth paying for. the
machinery stays private; the receipts are always shown.

## where candidates come from

this is the part that decides whether the product works, so it is written first.
a scoring engine with nothing to score is worth nothing.

three tiers, in the order we should invest in them:

- **customer-supplied lists** — a csv or an existing crm export the customer
  already owns. cheapest, already legal, and the customer trusts the names.
  every customer gets this on day one.
- **public authoritative sources** — public tender awards, trade registries,
  customs bulletins where they are published per company. these can *name* a
  company and be cited, which is what we need. free or cheap.
- **licensed commercial data** — a paid trade-data or company-data subscription.
  the reason to buy one is not that import volume and headcount are otherwise
  unobtainable — agentic research reaches them — but that a subscription answers
  in one call what a deep search spends minutes and many fetches on, for every
  company at once. buy it when the search bill for a criterion exceeds the
  subscription.

what does not count as a source: market aggregates that report country- or
hs-code-level totals without naming a company, and directories of events rather
than the companies attending them. they cannot produce a lead at any level of
effort, so they must not appear in the source list at all — a source that is
permanently unavailable reads to the customer as a broken product.

a source is only listed for a customer when an adapter for it actually runs. for
each source we record what fields it can emit, so the scoring engine knows what
it can and cannot ask for.

## selecting what to research

two passes at very different prices, and the difference matters more than any
other cost decision in the product.

**the cheap pass runs over everything.** country, current corpus version, and
whether any of the range's terms appear in what the corpus knows about that row.
this is a scan, it is cheap, and every candidate gets it — so nothing is
invisible to us, and a company we chose not to pursue is a company we can still
account for.

that scan should happen **once per corpus, at import**, not once per run. the
tokens for each row are computed and stored when the file lands, so selection on
every later run is an index lookup rather than a fresh pass over the same rows.
ten campaigns into poland currently scan the same rows ten times. it also means
that when a playbook gains new terms, we re-scan once instead of paying for a
wider match on every run forever.

**the expensive pass is gated.** agentic deep research is what costs real money,
so it only runs when something suggests it is worth it, in this order:

1. **what the shared pool already holds.** free, and the strongest signal there
   is: a company already validated as a built-in oven distributor is selected
   for every oven seller from then on, and never researched again.
2. **the corpus row**, as matched above.
3. **one cheap verification** for a row neither of the first two can judge,
   before committing to the expensive pass.

this gate is a control on *first contact* with a company, and it loosens itself
over time — every company researched for one customer is free for the next, so
the more the pool fills the less the gate has to refuse.

**what we store is wide; what we show is narrow.** these are different
operations and conflating them is expensive. researching a polish distributor
teaches us its store count, that it imports white goods *and* small kitchen
appliances, its brands, its contact domain, a tender it won. all of that is
stored against that company, including the parts irrelevant to whoever asked —
because when the next customer arrives, that is the difference between a free
answer and paying for the same pages twice.

the view is the opposite. **a customer sees only what their range covers.** a
food-packaging seller never sees an oven distributor in their list. the scope is
per campaign, not per customer, so a company selling both ovens and packaging
runs two campaigns without either leaking into the other.

the cost of being strict is that a wrong range is invisible — the customer
cannot see what they were not shown, and labels are hidden too. so admin gets an
"excluded by range" count on every run. that number is the only place a
mis-mapped range will ever appear.

## scoring

the score is computed from evidence, not from the search hint that surfaced the
company. a name matching a keyword in a list is a reason to look, never a reason
to score.

each criterion is earned by a claim that carries a source we can show. degree
matters: one mention in one place scores well below the same fact corroborated
by two independent sources. a criterion with no evidence is recorded as unknown,
not as zero and not as an assumed average — the difference between "we checked
and it's low" and "nobody would tell us" is the whole product.

**validated evidence is what a criterion is worth; everything else can only add
to it.** a fact is validated when a publisher with standing vouched for it, and
"standing" is decided mechanically, never by an agent's impression:

- **official** — the page is on the domain we resolved as that company's own.
- **registry** — the publisher is declared authoritative in the source catalog.
- **everything else is not validated**, however credible it looked.

credible press sits deliberately outside that line. a news article is the press
vouching, not the company, so it can *promote* a fact by agreeing with another
source but never validate one alone. this matters because validation is what
lets a fact into the shared pool: if it ever comes to mean "the agent found it
convincing", every customer inherits that judgement at once.

so a criterion's score is set by its strongest *validated* claim, and every
further claim, validated or not, adds a bounded share of what is left. an
unvalidated claim can raise a criterion. it can never define one while a
validated claim exists, and on its own it cannot reach the top of the scale.

**more evidence must never lower a score.** this sounds obvious and was not
true: the scorer averaged the claims in a criterion, so a corroborating web
mention next to an official fact dropped that criterion from 78 to 54. the
system was penalised for looking harder — which would have made the web-search
fallback actively harmful, since it exists precisely to produce more claims.
combining rather than averaging is what makes the fallback safe to turn on.

**every criterion has a path to an answer.** a criterion no structured source
covers is not dropped and not warned about — it goes to agentic research (next
section). a structured source is preferred when we have one because it is
cheaper and stronger, but the user's weights are honoured either way. what we
never do is accept a weight and then quietly ignore it.

**the honesty rule.** it still applies, one step later. when we have looked —
structured sources and agentic research both — and still cannot answer a
criterion for a given company, the lead says which criteria came back unknown
and how much of the user's weight they carried. a fit score computed from half
the user's criteria is a real answer, but only if it says so.

alongside the fit score, every lead carries a confidence figure: how much of
what we wanted to know we actually learned, how authoritative the sources were,
how fresh, and whether anything contradicted anything else. fit and confidence
are two separate numbers and are never averaged into one. a lead can be a
perfect fit on thin evidence, and the user needs to see that rather than have it
hidden inside a single number.

research runs once per company and is saved with its evidence. a rerun reuses
evidence that is still fresh — freshness is per source, since a tender notice
goes stale in a week and a company's own about page does not — and only re-fetches
what expired. if a company has never been researched, the agents do a deeper
pass aimed specifically at what is still unknown for that sector, rather than
re-asking what we already know.

## unknown specs: agentic research, cached

we will keep integrating data sources, and we will never finish. so for any
criterion no configured source can answer, agents go and find it on the web: a
deep search, following through to the company's own pages, filings, trade press
and local-language sources, and into the web archive when the live page is gone
or when we need what a page said last year.

the result is saved to the database against the resolved company, with its
source, the date it was retrieved and how it was obtained. every run after that
reads the answer instead of paying for the search again. the same company turns
up in campaign after campaign, so the corpus compounds: each run is faster,
cheaper and steadier than the one before, and a customer's second month costs a
fraction of their first.

four things this needs to get right, or the cache turns into a liability:

- **a failed search is a result too.** "we searched for this company's import
  volume and the web does not say" gets saved exactly like an answer does.
  otherwise we re-pay for the same fruitless deep search on every single run,
  which is the most expensive thing in the whole pipeline.
- **web-search evidence is weaker than an official source, and scores that way.**
  a fact an agent read on a trade-press page is real evidence and belongs in the
  score, but it does not carry the authority of a company's own filing. how a
  fact was obtained is stored with it and feeds the confidence figure, so the
  fallback can never quietly inflate a lead above one verified properly.
- **facts age at different speeds.** a company's founding year is permanent; its
  headcount is good for a year; an active tender is good for a week. each cached
  fact carries its own expiry, and only the expired ones are re-researched.
- **the archive is for history, not for now.** a snapshot from 2023 is excellent
  evidence about 2023 and must never be presented as the current state. an
  archived fact is labelled with the date of the snapshot, not the date we
  fetched it.

**one pool, all customers.** a public fact learned about a company for one
customer is available to every customer. we do not research the same company
twice because two customers happened to ask. this is the whole economics of the
product: the corpus is a company-wide asset that grows with every run, so each
new customer starts cheaper and faster than the last one did, and popular
markets become nearly free to serve.

**only validated facts are shared.** an unvalidated fact stays with the customer
whose run paid for it. the reasoning is asymmetric risk: a wrong fact used by
one customer is a bad lead, and a wrong fact copied to every customer is a
recall — we would have to find and fix it everywhere, and until we did, every
score built on it would be wrong. so the pool holds only what a publisher with
standing vouched for, keyed on resolved company identity and carrying its
provenance, and unvalidated findings stay local until something validates them.

this also gives the deep pass a second job worth doing: a fact we already hold
unvalidated is a cheap, specific question to put to an authoritative source, and
answering it promotes the fact into the shared pool for everybody.

**admin review is oversight, not a gate.** admins review company information in
the dashboard, and the system warns them when something is worth a look — a
profile built from thin evidence, a fact that many customers are now scoring
against, a company whose facts changed sharply since last time. nothing waits on
that review. the pipeline validates mechanically and runs; the warning is how a
person finds out in time to intervene.

that is a deliberate trade: we get throughput and no human bottleneck, and in
exchange nothing stops a bad fact before it spreads. so the correction path is
what has to be good, not the approval path. a withdrawn source or a corrected
fact must reach every customer holding it, promptly and without anyone having to
ask — which is the rule already written above, and this is why it is not
optional. an admin who spots a wrong fact needs one action that fixes it
everywhere.

what stays private to the tenant that owns it:

- **customer-supplied rows.** an uploaded list or a crm export is the customer's
  own asset, and their prospect list is competitive information. a fact we
  later learn about one of those companies from the public web is shared; the
  fact that the customer named it is not.
- **scores, weights and lead lists.** the shared pool holds facts. the score is
  computed per campaign from that customer's own weights and never travels.
- **opt-outs.** a person who told one customer to stop was talking to that
  customer. suppression stays tenant-scoped, and a shared fact can never
  override it.

because a fact is now shared, a fact being wrong is shared too. a withdrawn
source or a corrected fact propagates to every customer holding it, rather than
being fixed for the one who complained.

the deep pass is aimed only at what is still unknown for that company and that
sector. it never re-asks what we already hold — for anyone.

## language: english is the backbone

the interface is the customer's language — turkish first, and the admin
dashboard mainly english with turkish supported. everything that reaches the
backend and the database is english. a company's own documents can be in any
language; an agent reads them, fills out the company record in english, and the
webui renders it back in whatever language the viewer is using. that is what
makes one shared pool possible at all: a fact stored in the language it was
found in cannot be matched, compared or reused by anybody who did not happen to
search in that language.

**the agent does the translating**, at the point it extracts the fact. one step,
no second pass, and the original stays attached — which is the audit trail, so a
separate reviewable translation stage would be paying twice for the same
assurance.

**display translation is not the same problem as storage.** categories, roles,
labels and criteria are a fixed vocabulary: translate them once, ship the
dictionary, done. free-text facts are not, and re-translating them on every page
view is both expensive and unstable — the same fact would read differently on two
visits. so translated strings are cached with the fact, and the english stays
canonical underneath.

four rules make the rest work rather than quietly lose information.

**we store in english and search in the local language.** these are opposite
directions and confusing them breaks discovery outright. you cannot find a
polish distributor by searching for `oven` — the web there says `piekarnik`, the
german one says `Backofen`, and a term that matches nothing looks exactly like a
market with no buyers in it. so the query layer is multilingual and the storage
layer is not: each term in a sector playbook carries its equivalents in the
markets we sell into, we search with those, and we write down what we found in
english. for built-in kitchen appliances that is a small one-time list — oven,
hob, hood, built-in, cooker and their local equivalents.

**the original text is never discarded.** evidence keeps the source span exactly
as published, in its own language, alongside the english. this is not
sentimentality: the quote check that stops an agent inventing facts compares the
quote against the page we fetched, and that page is in polish — so the check has
to run against the original. it is also how any dispute about a fact gets
settled, and with one shared pool a mistranslation would otherwise propagate to
every customer with nothing to check it against.

**names are not translated.** company names, brand names, legal forms and
addresses stay as published, diacritics intact. only categories, roles and facts
are rendered into english. and where a local term has no clean english
equivalent — a licence class, a legal form — we keep the original as the value
and gloss it rather than inventing an approximation.

translation is processing, not sourcing. it cannot make a fact validated and it
cannot take validation away: an official polish page is an official source
whether we read it in polish or in english.

corpus imports are normalised the same way. the customer's own file is never
rewritten — their rows stay exactly as supplied — and the english tokens we match
against are built beside it at import.

## contact discovery

runs on demand for a company the user already wants, never automatically for
every lead. it costs money per company and most leads never get contacted.

1. if we already have an address for that company in the database, we use it.
2. otherwise we look for people holding buying-decision titles at the company —
   the company's own team pages, public professional profiles, press mentions,
   trade registries. a licensed people-data source is used when the customer has
   one configured.
3. if we have a name and a title but no address, we derive one from the company's
   observed address structure and label it as derived.
4. a generic info@ address is a fallback we record, not a result we celebrate.

**what the label means.** three tiers, and each tier is defined by what we
actually did, not by how confident an agent felt:

- **green** — the address came from a source that published it, or from the
  customer's own records.
- **yellow** — the address was derived from an observed pattern at that domain,
  and the domain accepts mail. the person and title are confirmed; the address
  is inferred.
- **red** — anything else: no pattern observed, the domain does not accept mail
  or accepts everything indiscriminately, or the person could not be confirmed.

the tier is stored on the contact and shown in the ui. outreach treats yellow as
bounce risk and never puts a yellow or red address in cc. we never send a
verification email to check an address — discovery does not contact anyone.

## outreach language

storage being english does not mean the message is. a polish purchasing manager
gets polish.

customers define their own email templates, one per language they sell in. when
they choose a language for a campaign, we use that template, and any custom text
they add for that send is written in the same language. the message is composed
at send time from english facts into the chosen language — the facts are
canonical, the wording is local.

one existing guard is worth knowing the shape of: outreach qa already fails a
message when turkish characters appear in a message not marked turkish, which
catches the operator's own language leaking into a foreign-language send. that is
the right check for this product and it is one-directional — it will not notice
english boilerplate left inside a polish template. per-language checks are worth
adding as each template language is added, not before.

## what the user ends up with

leads, ranked by their own weights, each with its score, its confidence, the
evidence behind each criterion, and the contacts we found with their tiers. it
all lands in the client's lead list, ready for the email and whatsapp campaigns
later. the admin sees the same data for every tenant.

## faster, cheaper, more accurate

the corpus growing is what makes runs cheaper. these are the things that make a
run *better*, in the order they are worth doing.

**accuracy**

- **combine, don't average** (above). nothing else on this list is safe until
  this is done, because everything else adds claims.
- **local-language terms per playbook.** the largest accuracy hole in the
  product, and it is upstream of every other one: an english term matched
  against a foreign corpus selects nothing and reads as an empty market. see
  language, above. one list per sector, written once.
- **match on word boundaries, not raw substring.** `oven` currently matches
  `ovenware`, which is a housewares retailer and not a buyer of appliances.
  small change, tightens selection in both directions.
- **a fact must quote its source, and we check the quote.** every agentic fact
  carries the exact span it came from and the url, and we reject it if that span
  is not in the page we stored. this is a substring check, not a judgement call.
  it is the one guard the agentic path cannot ship without: an agent inventing a
  plausible company with plausible numbers is the failure that ends a customer
  relationship, and it is invisible without this.
- **freshness has to be measured.** it was a constant — every lead with any
  evidence at all scored as equally fresh. a cache that keeps facts for months
  makes that the most dangerous number in the score, so each fact ages against
  its own shelf life (a founding year never expires, a headcount lasts a year,
  an open tender a week) and confidence reads the result.
- **a disagreement is information before it is a penalty.** when two sources
  conflict, the validated one wins, and on equal standing the newer one wins.
  only a disagreement between equally authoritative, equally recent sources is
  unresolvable, and only that one should cost confidence. with a long-lived
  cache, stale-versus-fresh becomes the ordinary case, and reading every one of
  those as unreliability would make the cache look like a defect.
- **a hard negative vetoes, it does not subtract.** a closed company, a
  manufacturer where we need a buyer, a sanctioned entity. these end the
  assessment. a dissolved company with excellent product fit must be gone from
  the list, not ranked fourth on it.
- **measure the score against what actually happened.** we already store sends
  and replies. report conversion per band: if band a and band c convert the
  same, the weights or the criteria are wrong, and no amount of evidence rigour
  fixes that. start as a report, not as an automatic adjustment — auto-tuning
  weights needs hundreds of outcomes before it is signal rather than noise.

**efficiency**

- **don't deep-research what a deep pass cannot change.** three cases end the
  work early and provably lose nothing: a terminal negative (closed, excluded,
  sanctioned), nothing left that any configured source could answer, and a lead
  already at the top band with no required evidence missing.
  worth being precise about what this is *not*: we cannot skip a company because
  it "can't reach band c", because more evidence only ever raises a score now —
  so there is no sound ceiling to compare against. the prune is on companies
  where there is nothing left to learn, not on companies that look unpromising.
- **scan the corpus at import, not per run.** selection re-matches the same rows
  on every campaign. computing each row's tokens once when the file lands turns
  every later run into an index lookup, and makes adding playbook terms a
  one-time re-scan instead of a permanent per-run cost.
- **plan the gaps in one batch.** resolve identities first, pull everything the
  pool already knows in one read, then plan searches against the complete gap
  list. one about page usually answers headcount, markets and product lines
  together; planning per criterion pays for that page three times.
- **cheap model extracts, good model decides.** the deep pass both chooses where
  to look (judgement) and pulls a number off a fetched page (not judgement).
  extraction is nearly all of the token volume and belongs on a small model,
  escalating only on disagreement. *needs the agentic extraction path to exist
  first — there is nothing to route today.*
- **refresh expiring facts before the customer asks.** the "every run is faster"
  promise only holds if the cache is warm when they press go. re-research what
  is about to expire off the critical path. *needs a scheduled job; the digest
  scheduler is the place to hang it.*

**latency**

- **stream leads as they qualify.** a five-hundred-company campaign shows
  nothing until it finishes. results are already written one company at a time,
  so the data is there — this is a polling endpoint and a list that grows. it
  makes nothing faster and makes the product feel like it works.

## rules that hold everywhere

- **identity.** two records for the same company must resolve to one lead. a
  lead is deduplicated on resolved identity, not on the spelling of its name.
- **compliance.** contact data is personal data. we honour opt-outs at the
  address level, tenant-wide, so re-importing a list cannot resurrect someone
  who asked us to stop. we do not use scraping wrappers or logged-in session
  automation against professional networks.
- **cost.** every run reports what it spent — requests per source, companies
  researched, contacts derived. a campaign is cancellable mid-run and stops
  spending when cancelled. per-source concurrency limits are respected, because
  a free source we hammer becomes a source we no longer have.
- **failure is visible.** a search term that matches nothing says so. a source
  that is down says so. an import whose file we cannot read says so. a campaign
  that returns nothing must tell the user why, and never look like a campaign
  that found nothing because there was nothing to find.

## how coverage grows

sector coverage is expected to be narrow at the start and is not a defect. we
begin with built-in kitchen appliances, from a corpus we own and a sector we
understand, and there is already a playbook for it.

what makes a new sector work is not engineering — the engine does not know what
it is selling — it is a playbook: which facts matter for that sector, which terms
name its products, and what those terms are called in the markets we sell into.
that is writing, not building, and it is the cheapest high-value work available.
the profile labels tell us which sector to write next, because they say what our
customers actually sell.

## open decisions

- which licensed trade-data provider we buy, and when. this is now a cost
  question rather than a blocker: agentic research covers these criteria from
  day one, and the search bill for import volume is the number that tells us
  whether a subscription pays for itself.
- how much a single company's deep pass is allowed to cost before it gives up
  and records the criterion as unknown. without a ceiling, one obscure company
  can eat a campaign's budget.
