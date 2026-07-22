# Obsidian-minnesprovider — kravspec (design)

**Datum:** 2026-07-22
**Status:** Godkänd inriktning, redo för implementeringsplan
**Kontext:** Ersätter det tidigare (och nu delvis trasiga) script-baserade
vault→MEMORY.md-systemet med en riktig extern `MemoryProvider`.

## Bakgrund & premiss

Användaren har redan ett Obsidian-valv (`/srv/dj/obsidian`, git-synkat till
`github.com:jaansson/obsidian`) som fungerar som källa av sanning för Hermes
minne. Ett script (`obsidian_memory_sync.py`) synkade `memory/*.md` in i de
inbyggda, teckenbegränsade MEMORY.md/USER.md. Root→hermes-migrationen
strandsatte automationen (scriptet ligger i `/root/.hermes/scripts/`,
oåtkomligt för hermes-gatewayerna). Git-synken (`/opt/obsidian/sync.sh`) var
borta — **redan återställd** i denna session.

Den script-baserade modellen har ett arkitektoniskt tak: allt måste rymmas i
~1300 tecken, ingen frågerelevans, hel-fil-in-eller-inget. Detta ersätts.

## Låsta designbeslut

1. **Ändamålsbyggd provider**, inte byggd på `holographic` (som äger sin egen
   store, inte kan ingesta valv-filer, och vars HRR-lager är avstängt på
   runtime pga saknad numpy).
2. **FTS5/BM25 som retrieval-bas.** Runtime-venv:et (Python 3.11) har bara
   stdlib — ingen numpy/torch/sentence-transformers — och är förseglat
   (`HERMES_DISABLE_LAZY_INSTALLS=1`), så tunga embedding-installationer är
   opålitliga. FTS5 finns i stdlib sqlite3 och är bevisat i drift.
3. **Semantiska embeddings (MiniLM) = opt-in-lager ovanpå FTS5**, bakom
   `is_available()`-grind med automatisk fallback. Byggs först om lexikalisk
   sökning bevisat inte räcker — inte spekulativt.
4. **Valvet = källa av sanning.** SQLite-indexet är ett raderbart derivat,
   nyckat på `path + mtime + content_hash`. Kan alltid byggas om från `.md`.
5. **Hemlighets-scrubbing före varje skrivning** till valvet (pushas till
   GitHub). Icke förhandlingsbart.

## Arkitektur

### Provider
`plugins/memory/obsidian/` som ärver `agent/memory_provider.py::MemoryProvider`.
Aktiveras med `memory.provider: obsidian` per profil.

Obligatoriskt: `name`, `is_available`, `initialize`, `get_tool_schemas`.
Kärn-hooks: `prefetch(query)` (per-turn recall), `system_prompt_block()`
(het kärna), `get_config_schema`/`save_config`, `backup_paths()`.
`sync_turn` = no-op (agenten skriver inte till valvet per turn).

### Index (derivat, raderbart)
- SQLite-fil utanför valvet (t.ex. `$HERMES_HOME/obsidian_index.db`), deklareras
  i `backup_paths()`. Ligger EJ i valvet → pushas aldrig till GitHub.
- Schema: `chunks(path, chunk_id, heading_trail, content, mtime, content_hash)`
  + FTS5-shadowtabell `chunks_fts` synkad via triggers (mönster från
  `plugins/memory/holographic/store.py:48-66`).
- **Chunkning:** strippa YAML-frontmatter; dela per rubrik (H1/H2-sektioner);
  varje chunk bär `path` + rubrik-spår för citat. Små noter = en chunk.
- **Inkrementell sync:** walk valvet (glob `*.md`, exkludera `.git`,
  `.obsidian`, `.trash`), jämför `content_hash` mot indexet; re-indexera bara
  ändrade filer, radera rader för borttagna filer. Körs vid `initialize` och
  på intervall/ändringsdetektering. Git-pull av valvet påverkar inte indexet
  (vi diffar mot filsystemet).
- **Scope:** hela valvet default; konfigurerbar exkluderingslista.

### Retrieval (`prefetch`)
- FTS5 `MATCH` på turn-queryn med query-sanitizer (OR-join av tokens,
  droppa stopwords — porta `retrieval.py:585-619` som referens; löser FTS5:s
  default-AND-recall-problem). BM25-rank, top-k chunks.
- Returnera formaterad text: per träff en rubrik `[[path#heading]]` + chunk-
  innehåll, för injektion i turn-kontexten. Snabbt (lokalt, ms).

### Het kärna (tvånivå)
- En liten "pinnad" not (t.ex. `memory/core.md` + `memory/daniel.md`) injiceras
  alltid via `system_prompt_block()` — prompt-cache-vänligt, alltid-relevant
  identitet.
- Allt annat i valvet når kontexten via `prefetch` (frågerelevant).
- **Detta ersätter `obsidian_memory_sync.py`s vault→MEMORY.md-roll.** Öppen
  fråga (spec-review): behåller vi de inbyggda MEMORY.md/USER.md för agentens
  egna session-skrivningar, eller styr vi allt genom valvet? Rekommendation:
  Fas A låter inbyggt minne vara orört (read-mostly provider); Fas B avgör
  konsolidering.

## Faser (en i taget, verifierad innan nästa)

**Fas A — Retrieval (högst värde, lägst risk):**
Provider med index-bygge + inkrementell sync + `prefetch` (FTS5). Verifierbart
utan modellanrop: index en fixtur-katalog, kör query, asserta rätt chunks.
Aktivera på EN profil, verifiera recall live. Inbyggt minne orört.

**Fas B — Skriv-tillbaka + het kärna:**
`system_prompt_block` från pinnad not. Väg för agenten att skriva lärda fakta
till valv-noter med hemlighets-scrubbing (`redact_sensitive_text`) + spärr mot
skrivning till gitignore:ade secret-sökvägar. Beslut om `obsidian_memory_sync.py`
pensioneras.

**Fas C — Semantisk uppgradering (opt-in):**
MiniLM (all-MiniLM-L6-v2, CPU) bakom `is_available()` + ny `LAZY_DEPS`-entry.
Hybrid FTS5+vektor (två-stegs, som `retrieval.py:48-112` men inlärd modell).
Fallback till ren FTS5 när modellen ej installerad. Endast om Fas A visar att
lexikalisk recall inte räcker.

## Parallellt: återställ strandsatt Obsidian-automation till hermes

Skilt från providern, men samma ekosystem — migrationen tog sönder:
- **Daily-note-generering** (dagens 2026-07-22 saknas) — Hermes-cronjobb som
  kör morgon-rutinen; flytta/koppla om till hermes.
- **`kunskapsgraf.py`** — grafgenerator; flytta till hermes + schemalägg.
- `OBSIDIAN_VAULT_PATH` saknas i hermes `.env` — sätt den.

**Utanför scope (flaggat):** de ~20 övriga strandsatta scripten i
`/root/.hermes/scripts/` (trading, mail, påminnelser) — separat städjobb.

## Testning
- Chunkning, sanitizer, hash-diff-sync = rena funktioner, testbara utan modell.
- `prefetch` mot en fixtur-vault: query → förväntade chunks (FTS5, deterministiskt).
- Secret-scrubbing: känd hemlighet i skriv-input → scrubbad/vägrad.
- Alla retrieval-beslut verifierbara utan modellanrop (spec-krav, som routern).

## Risker & avvägningar
- **Lexikalisk sökning missar synonymer/parafraser** — mildras av Fas C om det
  visar sig i praktiken. Börja lexikalt (robust), mät, uppgradera vid behov.
- **En extern provider åt gången** — Obsidian blir den; ingen förlust (Honcho/
  mem0 används ej).
- **Index-drift** — löst av path+mtime+hash och att indexet alltid kan raderas
  och byggas om från valvet.
