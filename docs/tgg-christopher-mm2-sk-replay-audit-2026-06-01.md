# TGG Christopher MM2-SK Replay Audit - 2026-06-01

## Run Evidence

- Run id: `tgg-mm2-sk-resolver-proof-20260601-214343`
- Chat: `120363403845802098@g.us` / MM2 Maintenance (SK)
- Window: from `2026-05-24 00:00:00 SGT`
- Replay input: 157 WhatsApp bridge messages
- Turn policy: 5 minute quiet-window debounce, direct mention immediate
- Output: 28 Hermes turns, one Christopher session: `20260601_214343_339e6496`
- Business bridge: local copied-DB operator backend only, `http://127.0.0.1:64086`
- Replay execution mutation: none. After approval, the replay rows were copied into the production review-only `tgg_christopher_*` tables from a DB backup point; production case rows were not touched.
- Standalone review artifact: `/Users/pcloffice/pcl-docs/records/tgg-mm2-sk-resolver-proof-20260601-214343.html`
- MBA copy opened at: `/tmp/tgg-mm2-sk-resolver-proof-20260601-214343.html`

## Cost And Call Visibility

- Captured main-model Responses calls: 99
- Captured main-model usage: 6,605,597 input tokens, 6,283,776 cached input tokens, 23,712 output tokens, 8,518 reasoning output tokens
- Main-model subtotal: `$0.81935295` using `gpt-5.4-mini` pricing
- Gemini vision calls completed: 100, from `agent.log`
- Gemini usage/cost is not yet captured by the native vision path; the HTML includes this caveat.

## What Improved

The address-first case lookup fix worked on the exact class of failures that triggered this work.

- Turn 1, Blk 350 Anchorvale Rd #11-109:
  - Now debounced into one turn with all before/after photos and completion text.
  - Christopher searched by address only.
  - It attached the update to `SK/JOB/2604/2376`.

- Turn 7, Blk 446A Jalan Kayu #04-316:
  - Christopher searched `Blk 446A Jalan Kayu #04-316`, not `toilet door main gate`.
  - It found and updated `SK/JOB/2605/1415`.
  - It separately handled the same burst's `#03-326` address instead of mixing the two addresses.

- Turn 10, Blk 182 Rivervale Crescent #17-315:
  - Christopher searched the address first.
  - It found and updated `SK/JOB/2603/1728`.

- Turn 21, SK/JOB/2510/3107 / Blk 463B Sengkang West Way #08-241:
  - The new-job registration and the follow-up completion are in the same debounced turn.
  - Christopher used the first message's job number as context and attached the completion to `SK/JOB/2510/3107`.

- Turn 22, work-cast note for Blk 446A Jalan Kayu #04-316:
  - Christopher logged WC `SK/WC/2605/0496` against `SK/JOB/2605/1415`.

## Remaining Issues

- The replay still needs a provider-agnostic call ledger. Main model calls are captured from OpenAI Responses dumps; Gemini native vision calls are only counted from logs.
- The local copied-DB backend currently creates generated `WA/JOB/2606/*` numbers. In one long run, earlier generated rows became invisible later because the backend queries used read-only connections while SQLite sidecars were active. The safer contract now refuses to start a replay when copied DB sidecars already exist.
- Some cases are missing from the copied DB, so Christopher creates WhatsApp-only scratch cases:
  - `SK/JOB/2605/2439`
  - `SK/JOB/2605/2480`
  - `SK/JOB/2605/2564`
  - `SK/JOB/2605/2634`
  - `SK/JOB/2605/2705`
  - `SK/JOB/2605/2708`
  - `SK/JOB/2605/2847`
- The Anchorvale ESS turns still over-search and create scratch cases. This looks like an address normalization/resolver issue around `Anchorvale Road ESS` / `Anchorvale St ESS`, not the old work-type-in-address bug.
- Turn 19 still tries generic `pa_business_write` / `pa_business_read` after lookup/search failure. Christopher's constitution should steer it back to `tgg_case_*` only for this replay/live workflow.

## Harness Hardening Added

- Default replay profile: `tgg-local-gpt54-mini-gemini-vision`
- Profile pins:
  - main provider/model: `openai-direct-primary` / `gpt-5.4-mini`
  - vision provider/model: `gemini` / `gemini-3.1-flash-lite`
  - vision concurrency: 8
  - business mode: copied-DB local operator
  - debounce: 300 seconds
  - pricing required
- Preflight now rejects:
  - production business URLs
  - copied DBs with pre-existing SQLite sidecars
  - missing copied DB
  - failed copied-DB integrity check
  - missing pricing for configured models

## Next Work

- Add provider-agnostic JSONL call ledger for Gemini and OpenAI calls.
- Make scratch case creation visible in a stable replay table or keep a single read/write SQLite connection for the local backend.
- Tighten Christopher's tool mandate so `pa_business_*` generic routes are not used in this workflow.
- Add resolver normalization for ESS/common-area addresses.
