# Obsidian-minnesprovider Fas B — kravspec (design)

**Datum:** 2026-07-22
**Status:** Godkänd inriktning, redo för implementeringsplan
**Föregående:** Fas A (retrieval) — mergad, live på coding-profilen.

## Mål

Fyra delar ovanpå Fas A:s FTS5-retrieval:
1. **Interval-re-sync** — indexet uppdateras löpande, inte bara vid start.
2. **Het kärna** (`system_prompt_block`) — pinnade noter alltid i kontexten.
3. **Skriv-tillbaka** — dedikerat verktyg som skriver lärda fakta till valvet.
4. **Pensionera** `obsidian_memory_sync.py` — ersatt av provider + het kärna.

## Låsta designbeslut

1. **Re-sync = bakgrundstråd** i providern (inte lazy-i-prefetch), så prefetch
   förblir snabb och aldrig blockeras av en sync under manager-timeouten.
2. **Skriv-tillbaka = dedikerat verktyg** `obsidian_remember`, inte spegling av
   `memory()`. Agenten sparar medvetet.
3. **Hemlighets-scrubbing före varje skrivning** via `agent/redact.py::
   redact_sensitive_text(..., force=True)` + vägran att skriva till
   gitignore:ade secret-sökvägar. Icke förhandlingsbart (valvet → GitHub).
4. **Valvet förblir källan.** Skriv-tillbaka går till en hermes-namnrymd
   (`<vault>/hermes/`) skild från användarens handkurerade noter.

## Del 1 — Interval-re-sync

- `ObsidianIndex` / providern får en bakgrunds-daemon-tråd som anropar
  `sync_vault` var `sync_interval_minutes` (config, default 5). Använder den
  befintliga RLock:en (Fas A) — ingen ny låsning.
- Tråden startas i `initialize` (efter den första synkade indexeringen) och
  stoppas i `shutdown()` (rent, via en `threading.Event`).
- Fel i en sync-runda loggas (`logger.warning`) och sväljs — nästa runda
  försöker igen. Git-pull av valvet (via /opt/obsidian/sync.sh) fångas
  automatiskt eftersom vi diffar mot filsystemet.
- Config: `obsidian.sync_interval_minutes` (0 = av → Fas A-beteende).

## Del 2 — Het kärna (`system_prompt_block`)

- Override `system_prompt_block()` → läser `config.pinned`-noterna
  (redan parsad i Fas A), returnerar deras innehåll (frontmatter strippad)
  som ett block för systemprompten. Tom sträng om inga pinnade / oläsbara.
- Default-pin (config-exempel): `memory/daniel.md` (identitet). Användaren
  styr listan.
- Prompt-cache-vänligt: läses en gång per session (systemprompten byggs en
  gång). Kompletterar `prefetch` (den frågerelevanta svansen).
- Gräns per pinnad not (t.ex. 4000 tecken) så en stor not inte sväller
  prompten; trunkera med markör.

## Del 3 — Skriv-tillbaka (`obsidian_remember`)

- `get_tool_schemas()` returnerar EN verktygsdefinition `obsidian_remember`:
  - `content` (krävs) — faktumet att spara.
  - `title` (valfri) — annars härleds från innehållet/datum.
  - `tags` (valfri lista).
- `handle_tool_call("obsidian_remember", args)`:
  1. **Scrubba** `content` via `redact_sensitive_text(content, force=True)`.
     Om scrubbning ändrade texten (hemlighet fanns): skriv den scrubbade
     versionen och notera i verktygssvaret att en hemlighet maskerades.
     (Vägra hellre än att läcka — men scrubbning + notis är standard.)
  2. Bygg en atomisk notering: YAML-frontmatter (`created`, `source: hermes`,
     `tags`) + rubrik + innehåll.
  3. Skriv till `<vault>/hermes/<slug>-<timestamp>.md` (hermes-namnrymd).
     **Vägra** om målsökvägen matchar en gitignore:ad secret-sökväg eller
     ligger utanför valvet (path-traversal-skydd).
  4. Uppdatera indexet för den nya noten (`upsert_note`) så den är
     omedelbart sökbar.
  5. Returnera JSON: sparad sökväg + ev. scrubbnings-notis. Git-synken
     pushar noten inom 3 min.
- `sync_turn` förblir no-op (skrivning är verktygs-driven, inte per-turn).

## Del 4 — Pensionera `obsidian_memory_sync.py`

- Scriptet kör inte (strandsatt i /root/.hermes/scripts). Het kärnan ersätter
  dess vault→MEMORY.md-roll (identitet i prompten via pinnad not).
- Åtgärd: dokumentera att det är ersatt; ta bort dess cronjobb-post ur den
  gamla root-`jobs.json` (så det inte återupplivas vid ev. framtida migrering
  av de jobben). Radera INTE `.memory_hashes.json` (harmlös, gitignore:ad).
  Inget nytt bygge — ren städning + dokumentation.

## Testning

- Re-sync-tråd: testa att en ändrad fixtur-fil fångas efter ett sync-varv
  (anropa sync-metoden direkt; testa inte trådtiming). Testa `shutdown()`
  stoppar tråden.
- `system_prompt_block`: pinnad fixtur-not → innehåll i blocket; saknad →
  tom sträng; trunkering vid gräns.
- `obsidian_remember`: skriver atomisk not; **hemlighet i content → scrubbad
  i den skrivna filen** (kritiskt test); path-traversal/secret-sökväg → vägras;
  ny not omedelbart sökbar via `search`.
- Alla beslut verifierbara utan modellanrop.

## Risker & avvägningar

- **Re-sync-tråd + prefetch-tråd** delar RLock:en; en sync kan kort blockera
  en prefetch. Mildras av att sync är inkrementell (bara ändrade filer) och
  körs sällan (5 min). Manager-timeouten skyddar mot hängning.
- **Scrubbning är sista linjen** — inbox-namnrymden (`hermes/`) + din
  granskning i Obsidian/git är andra spärrar. `redact_sensitive_text` fångar
  kända mönster; exotiska hemligheter kan slippa igenom, därför separat
  namnrymd + git-historik för ångra.
- **Verktygs-yta:** `obsidian_remember` blir en ny extern-provider-tool; bara
  aktiv när `memory.provider: obsidian`.
