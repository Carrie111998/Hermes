# Working in this repo

`AGENTS.md` lines 1–70 are the repository map: which layer is the product,
which is inherited Hermes, where things run. Read that first and don't
re-derive it. This file is the part that isn't a map — what agents get wrong
here, and what burns tokens finding out.

---

## Spend tokens where the product is

The product is `server/`. It is small. The repo around it is not: fifteen
Python files exceed 100 KB and four exceed 300 KB (`gateway/run.py` is 973 KB,
`cli.py` 726 KB, `agent/conversation_loop.py` 295 KB). Those belong to the
inherited Hermes layer and are almost never what a product change touches.

**Never read a file to find out what is in it.** `grep -n` for the symbol,
then `sed -n 'START,ENDp'` on the hit. Reading `cli.py` once costs more than
most whole tasks.

**Ask the code, don't read it.** Runtime beats grep for anything the code can
answer about itself, and it is exact rather than approximate:

```bash
.venv/bin/python -c "
from server.app import create_app
for r in create_app().routes: print(getattr(r,'methods',None), r.path)" 2>/dev/null
```

(`create_app()` logs three warnings about unset `INTERFAZE_*` env vars on every
boot. They are expected outside a configured deployment — drop stderr rather
than chasing them.)

That is the correct way to answer "what routes exist" — not grepping
`@router` across seventeen files in `server/routes/`. The same trick answers
what a sector playbook holds (`load_sectors()`), what a tenant's source
catalog shows (`LeadResearchService(db).catalog(company_id)`), and what fields
a model requires (`Model.model_fields`).

**`.venv/bin/python`, not `python`.** The venv is the environment.

---

## Tests

```bash
.venv/bin/python -m pytest tests -q        # ~1050 tests, ~95s
```

`addopts = -m 'not integration'` is in `pyproject.toml`, so integration tests
are opt-in.

**A green suite tells you nothing about the agent.** Every server test injects
`StubRunExecutor`, which answers any run with empty lists — `{"leads": []}`,
`{"pages": [], "facts": [], "stop_reason": "source_exhausted"}`. If a test
"finds no leads", that is why, and it is not a bug. Exactly one test runs a
real agent:

```bash
.venv/bin/python -m pytest tests/integration/test_live_lead_research_gap.py -m integration
```

It skips when no LLM provider is configured, and says so.

**pytest is not the whole check.** CI also runs the webui suite, which pytest
never touches. Run both after any change under `server/webui`:

```bash
node --test tests/server/webui/*.mjs
node --input-type=module --check < server/webui/js/pages/<changed>.js
```

There is no bundler and no type checker on the JS, so a syntax error ships
unless `--check` catches it.

---

## Documents that are contracts

These are verified by tests. Change one side and the other fails — which is
the point, so fix both rather than editing the assertion.

| Document | Checked against | By |
|---|---|---|
| `PRODUCT.md` §7 (API routes) | the live OpenAPI schema | `tests/server/test_api_mvp.py` |
| `PRODUCT.md` §8.2 (frontend routes) | `server/webui/js/main.js` | `tests/server/test_webui.py` |
| `skills/sales/*/SKILL.md` | the run contract in `server/run_types.py` and `OUTPUT_KEYS` | `tests/server/`, `tests/sales_skills/` |

`skills/sales/*/SKILL.md` is behaviour, not documentation. Changing what a run
returns means changing the skill text, `run_types.py`, `OUTPUT_KEYS` and the
tests together.

**Generated files:** `skills/sales/lead-research/references/sectors.yaml` is
the source; `sectors.md` and `sectors.csv` are generated from it. After
editing the YAML:

```bash
.venv/bin/python -m server.lead_research.sectors          # regenerate
.venv/bin/python -m server.lead_research.sectors --check  # verify
```

Nothing in CI runs that check. Stale artifacts are silent — run it yourself.

**Two files named PRODUCT.md.** Root is the engineering spec (routes,
contracts, order of work). `server/webui/PRODUCT.md` is design context
(register, users, brand, accessibility). They are not versions of each other.

---

## Invariants that look like refactoring targets

Each of these exists because the obvious simpler version was wrong in
production, and each carries a comment saying so. They are product
commitments from `docs/product/lead-research-idea.md`. Do not collapse them.

- **Unknown is not zero, and not an assumed average.** A missing dimension is
  `None` and its weight is reported as unanswered (`scoring.derive_dimension_scores`,
  `known_weight`). "We checked and it's low" and "nobody would tell us" are
  different answers.
- **Fit and confidence are two numbers.** Never averaged into one. A perfect
  fit on thin evidence has to read as exactly that.
- **Combine, don't average.** More evidence must never lower a score
  (`scoring._combine`). Averaging claims dropped a criterion from 78 to 54 for
  finding a corroborating source, which made the web-search fallback harmful.
- **Every agentic fact quotes its source, and the quote is checked.**
  `quotes.accept_fact` rejects any fact whose span is not an exact substring of
  the stored snapshot. This is the one guard the agentic path cannot ship
  without; an invented company with invented numbers is invisible otherwise.
- **Hard negatives veto, they do not subtract.** A dissolved company is gone
  from the list, not ranked fourth (`verdicts.hard_negative`).
- **Freshness is per-field TTL.** A founding year never expires, a headcount
  lasts a year, an open tender a week. It was a constant once, and a long-lived
  cache made that the most dangerous number in the score.
- **A zero-result run names its reason.** `metrics.zero_result_explanation`
  returns a stable vocabulary, not prose the UI infers. A campaign that found
  nothing must never look like a market with nothing in it.
- **Storage is English, search is the market's language, names are never
  translated.** One shared fact pool only works if facts are comparable; `oven`
  selects nothing in Poland, where the web says `piekarnik`. Adding a target
  country means adding its terms to every sector in `sectors.yaml`, or
  `unmapped_markets` will report it.

---

## Gotchas that cost real time

- **`hermes -z` exits 0 when the agent fails.** "No LLM provider configured"
  and HTTP 401 both return status 0 with the reason on stdout. Never trust the
  return code; `HermesProcessExecutor` carries the transcript tail into the
  error for this reason.
- **Dead code is real here, and tests keep it alive.** Before assuming a page
  or module is live, check that something imports it:
  `grep -rn "thing.js" --include='*.js' . | grep -v "pages/thing.js"`. A test
  asserting on a file's contents is not a caller.
- **`data/` is gitignored and holds contact PII.** Real names, emails and phone
  numbers. Never commit it, never paste rows into output or a commit message.
- **Postgres is production; the suite runs SQLite.** Schema and dialect parity
  are enforced statically (`test_postgres_parity.py`, `test_postgres_backend.py`)
  and that coverage is good — but no test ever executes against a live
  Postgres. On a failure that only appears in production, suspect
  `server/postgres.py` and the migrations first.
- **`server/webui` has no build step.** Vanilla ES modules served by FastAPI.
  There is no `npm run build`, and adding one is a decision, not a fix.
- **A new table in `server/db.py` reaches production only via a migration.**
  Add the file under `server/supabase/migrations/`, wire it into
  `REQUIRED_MIGRATIONS`, and give it an RLS policy. The parity tests enforce
  all three, and have caught tables that existed only in SQLite.

---

## Before you say it's done

```bash
.venv/bin/python -m pytest tests -q
node --test tests/server/webui/*.mjs                      # if webui changed
.venv/bin/python -m server.lead_research.sectors --check   # if sectors.yaml changed
```

Report what actually ran. If the live agent test skipped, it skipped — a green
suite is not evidence that the agent works, and saying otherwise is the single
most expensive mistake available in this repo.
