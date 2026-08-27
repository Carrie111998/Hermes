---
title: Osint Investigation — Follow the money via public records and sanctions data
sidebar_label: Osint Investigation
description: Следите за деньгами через публичные записи и данные о санкциях
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Осинт Расследование

Следите за деньгами через публичные записи и данные о санкциях.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/research/osint-investigation` |
| Путь | `optional-skills/research/osint-investigation` |
| Версия | `0.1.0` |
| Автор | Агент Гермеса (адаптировано из ShinMegamiBoson/OpenPlanter, MIT) |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `osint`, `investigation`, `public-records`, `sec`, `sanctions`, `corporate-registry`, `property`, `courts`, `due-diligence`, `journalism` |
| Сопутствующие навыки | [`domain-intel`](/docs/user-guide/skills/optional/research/research-domain-intel), [`arxiv`](/docs/user-guide/skills/bundled/research/research-arxiv) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# OSINT-расследование — перекрестные ссылки на публичные записи

Система расследований публичных записей OSINT: государственные контракты,
корпоративные документы, лоббирование, санкции, утечки из офшоров, записи о собственности,
протоколы судебных заседаний, веб-архивы, базы знаний и глобальные новости. Решить
субъекты из разнородных источников, создавайте перекрестные ссылки с явным
уверенности, проводить статистические временные тесты и предоставлять структурированные доказательства
цепи.

**Только Python stdlib.** Нулевая установка. Работает на Linux, MacOS, Windows. Большинство
источники работают без ключа API (у OpenCorporates есть дополнительный бесплатный токен
это повышает лимиты ставок).

Адаптировано из проекта ShinMegamiBoson/OpenPlanter, лицензированного MIT; расширенный
для освещения личности/имущества/судебных разбирательств/архивов/источников новостей, которые
оригинал не адресован.

## Когда использовать этот навык

Используйте, когда пользователь запрашивает:

- «следить за деньгами» — госконтракты, лоббирование → законодательство, санкции
- корпоративная комплексная проверка — кто контролирует компанию X, где они находятся
  зарегистрированы, кто входит в их советы директоров, какие документы они подали
- проверка санкций — это организация X в OFAC SDN, офшорных утечках ICIJ
- расследование pay-to-play — подрядчики с оффшорными связями, лоббирование
  клиенты, получающие награды
- владение недвижимостью — найдите зарегистрированные акты/ипотеки по имени или адресу.
  (Нью-Йорк; для других округов укажите пользователям соответствующий рекордер)
- история судебных разбирательств — найдите решения федерального суда и суда штата, а также протоколы PACER.
- разрешение объекта с несколькими источниками, где наименование различается (суффиксы LLC, сокращения)
- построение цепочки доказательств с явными уровнями достоверности
- «что говорили об X» — международные новости (GDELT) + Википедия
  повествование + Wayback Machine для восстановления неработающих URL-адресов

НЕ используйте этот навык для:

- общие веб-исследования → `web_search` / `web_extract`
- OSINT домена/инфраструктуры → `domain-intel` навык
- научная литература → `arxiv` навык
- открытие профиля в социальных сетях → навык `sherlock` (необязательно)
- **федеральное** финансирование избирательных кампаний США — здесь намеренно НЕ рассматривается FEC.
  (API ненадежен для специальных запросов имени участника на бесплатной
  уровень DEMO_KEY). Для получения федеральных пожертвований укажите пользователям ссылку
  https://www.fec.gov/data/ напрямую.

## Рабочий процесс

Агент запускает сценарии с помощью инструмента `terminal`. `SKILL_DIR` — это каталог
владеющий этим SKILL.md.

### 1. Определите, какие источники применимы

Прочтите записи в вики-источнике данных, чтобы спланировать расследование:

```
ls SKILL_DIR/references/sources/

# Federal financial / regulatory
cat SKILL_DIR/references/sources/sec-edgar.md       # corporate filings
cat SKILL_DIR/references/sources/usaspending.md     # federal contracts
cat SKILL_DIR/references/sources/senate-ld.md       # lobbying
cat SKILL_DIR/references/sources/ofac-sdn.md        # sanctions
cat SKILL_DIR/references/sources/icij-offshore.md   # offshore leaks

# Identity / property / litigation / archives / news
cat SKILL_DIR/references/sources/nyc-acris.md       # NYC property records
cat SKILL_DIR/references/sources/opencorporates.md  # global corporate registry
cat SKILL_DIR/references/sources/courtlistener.md   # court records (federal + state)
cat SKILL_DIR/references/sources/wayback.md         # Wayback Machine archives
cat SKILL_DIR/references/sources/wikipedia.md       # Wikipedia + Wikidata
cat SKILL_DIR/references/sources/gdelt.md           # global news monitoring
```

Each entry follows a 9-section template: summary, access, schema, coverage,
cross-reference keys, data quality, acquisition, legal, references.

The **cross-reference potential** section maps join keys between sources — read
those first to pick the right pair.

### 2. Acquire data

Each source has a stdlib-only fetch script in `SKILL_DIR/scripts/`:

**Federal financial / regulatory**

```bash
# SEC EDGAR filings (corporate disclosures)
python3 SKILL_DIR/scripts/fetch_sec_edgar.py --cik 0000320193 \
    --types 10-K,10-Q --out data/edgar_filings.csv

# USAspending federal contracts
python3 SKILL_DIR/scripts/fetch_usaspending.py --recipient "EXAMPLE CORP" \
    --fy 2024 --out data/contracts.csv

# Senate LD-1 / LD-2 lobbying disclosures
python3 SKILL_DIR/scripts/fetch_senate_ld.py --client "EXAMPLE CORP" \
    --year 2024 --out data/lobbying.csv

# OFAC SDN sanctions list (full snapshot)
python3 SKILL_DIR/scripts/fetch_ofac_sdn.py --out data/ofac_sdn.csv

# ICIJ Offshore Leaks — downloads ~70 MB bulk CSV on first use,
# then searches it locally. Cached for 30 days under
# $HERMES_OSINT_CACHE/icij/ (default: ~/.cache/hermes-osint/icij/).
python3 SKILL_DIR/scripts/fetch_icij_offshore.py --entity "EXAMPLE CORP" \
    --out data/icij.csv
```

**Identity / property / litigation / archives / news**

```bash
# NYC property records (deeds, mortgages, liens) — ACRIS via Socrata
python3 SKILL_DIR/scripts/fetch_nyc_acris.py --name "SMITH, JOHN" \
    --out data/acris.csv
python3 SKILL_DIR/scripts/fetch_nyc_acris.py --address "571 HUDSON" \
    --out data/acris_addr.csv

# OpenCorporates — 130+ jurisdiction corporate registry
# (free token required; set OPENCORPORATES_API_TOKEN or pass --token)
python3 SKILL_DIR/scripts/fetch_opencorporates.py --query "Example Corp" \
    --jurisdiction us_ny --out data/opencorporates.csv

# CourtListener — federal + state court opinions, PACER dockets
python3 SKILL_DIR/scripts/fetch_courtlistener.py --query "Smith v. Example Corp" \
    --type opinions --out data/courts.csv

# Wayback Machine — historical web captures
python3 SKILL_DIR/scripts/fetch_wayback.py --url "example.com" \
    --match host --collapse digest --out data/wayback.csv

# Wikipedia + Wikidata — narrative bio + structured facts
# Set HERMES_OSINT_UA=your-app/1.0 (your@email) to identify yourself
python3 SKILL_DIR/scripts/fetch_wikipedia.py --query "Bill Gates" \
    --out data/wp.csv

# GDELT — global news in 100+ languages, ~2015→present
python3 SKILL_DIR/scripts/fetch_gdelt.py --query '"Example Corp"' \
    --timespan 1y --out data/gdelt.csv
```

All outputs are normalized CSV with a header row. Re-run scripts idempotently.

When a private individual won't be in a source (e.g. SEC EDGAR for a non-public-
company person, USAspending for someone who isn't a federal contractor, Senate
LDA for someone who isn't a lobbying client), the script returns 0 rows with a
clear warning rather than silently writing an empty CSV. EDGAR specifically
flags when the company-name resolver matched an individual Form 3/4/5 filer
rather than a corporate registrant.

Rate-limit notes are in each source's wiki entry. Default fetchers sleep
politely between paginated requests. **API keys raise rate limits** for
sources that support them (`SEC_USER_AGENT`, `SENATE_LDA_TOKEN`,
`OPENCORPORATES_API_TOKEN`, `COURTLISTENER_TOKEN`). All scripts surface
429 responses immediately with the upstream's quota message so the user
knows to slow down or supply a key.

### 3. Resolve entities across sources

Normalize names and find matches between two CSV files:

```bash
# Match lobbying clients (Senate LDA) against contract recipients (USAspending)
python3 SKILL_DIR/scripts/entity_resolution.py \
    --left  data/lobbying.csv   --left-name-col  client_name \
    --right data/contracts.csv  --right-name-col recipient_name \
    --out data/cross_links.csv
```

Three matching tiers with explicit confidence:

| Tier | Method | Confidence |
|------|--------|------------|
| `exact` | Normalized strings equal after suffix/punctuation strip | high |
| `fuzzy` | Sorted-token equality (word-bag match) | medium |
| `token_overlap` | ≥60% token overlap, ≥2 shared tokens, tokens ≥4 chars | low |

Output `cross_links.csv` columns: `match_type, confidence, left_name,
right_name, left_normalized, right_normalized, left_row, right_row`.

### 4. Statistical timing correlation (optional)

Test whether two time series cluster suspiciously close together — e.g.
lobbying filings near contract awards — using a permutation test:

```bash
python3 SKILL_DIR/scripts/timing_analysis.py \
    --donations data/lobbying.csv --donation-date-col filing_date \
        --donation-amount-col income --donation-donor-col client_name \
        --donation-recipient-col registrant_name \
    --contracts data/contracts.csv --contract-date-col award_date \
        --contract-vendor-col recipient_name \
    --cross-links data/cross_links.csv \
    --permutations 1000 \
    --out data/timing.json
```

The script's column flags are intentionally generic — the original tool was
written for donations vs awards, but it works for any (event, payee) time
series joined through cross-links. Null hypothesis: event timing is
independent of award dates. One-tailed p-value = fraction of permutations
with mean nearest-award distance ≤ observed. Minimum 3 events per (payer,
vendor) pair to run the test.

### 5. Build the findings JSON (evidence chain)

```bash
python3 SKILL_DIR/scripts/build_findings.py \
    --cross-links data/cross_links.csv \
    --timing data/timing.json \
    --out data/findings.json
```

Every finding has `id, title, severity, confidence, summary, evidence[], sources[]`.
Each evidence item points back to a specific row in a source CSV. The user (or a
follow-up agent) can verify every claim against its source.

## Confidence and evidence discipline

This is the load-bearing rule of the skill. Tell the user:

- Every claim must trace to a record. No naked assertions.
- Confidence tier travels with the claim. `match_type=fuzzy` is "probable",
  not "confirmed."
- Entity resolution produces candidates, NOT conclusions. A `fuzzy` match
  between "ACME LLC" and "Acme Holdings Group" is a lead, not a fact.
- Statistical significance ≠ wrongdoing. p &lt; 0.05 means the timing pattern
  is unlikely under the null. It does not establish corruption.
- All data sources here are public records. They may still contain
  inaccuracies, stale info, or redactions (GDPR, sealed records).

## Adding a new data source

Use the template:

```bash
cp SKILL_DIR/templates/source-template.md \
    SKILL_DIR/references/sources/<your-source>.md
```

Заполните все 9 разделов. Напишите сценарий `fetch_<source>.py` в `scripts/`, который
использует только stdlib и записывает нормализованный CSV. Обновите список источников в
Раздел «Когда использовать» выше.

## Инструменты и их ограничения

- `entity_resolution.py` НЕ использует внешние нечеткие библиотеки (без RapidFuzz,
  нет медуз). Сопоставление токенов с пакетами здесь является верхней границей. Если вам нужно
  Левенштейна, транслитерация или фонетическое сопоставление, pip-install отдельно.
- `timing_analysis.py` использует `random` Python для перестановок. Для
  воспроизводимость, пройти `--seed N`.
- Скрипты `fetch_*.py` используют `urllib.request` и учитывают `Retry-After`. Тяжелый
  массовое использование по-прежнему может нарушать Условия обслуживания — сначала прочтите юридический раздел каждого источника.

## Юридическое примечание

Все источники Фазы 1 являются общедоступными. Массовое приобретение разрешено в соответствии с
их соответствующие условия доступа (FOIA, закон о публичных записях, явные требования ICIJ).
публикация, общедоступные данные OFAC). Однако:

- Некоторые источники жестко ограничивают ставки. Уважайте их заголовки.
- Некоторое редактирование информации о владельце домена (GDPR по WHOIS, запечатанные документы).
- Перекрестные ссылки на публичные записи для идентификации частных лиц могут иметь
  этические последствия. Этот навык создает цепочки доказательств, а не обвинений.