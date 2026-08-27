---
title: 'Llm Wiki — LLM Wiki Карпат: сборка/запрос взаимосвязанной уценки, КБ'
sidebar_label: Llm Wiki
description: 'LLM Wiki Карпати: сборка/запрос взаимосвязанной уценки, КБ'
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Ллм вики

LLM Wiki Карпати: сборка/запрос взаимосвязанных КБ уценки.

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/research/llm-wiki` |
| Версия | `2.1.0` |
| Автор | Агент Гермес |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `wiki`, `knowledge-base`, `research`, `notes`, `markdown`, `rag-alternative` |
| Сопутствующие навыки | [`obsidian`](/docs/user-guide/skills/bundled/заметок/заметок-obsidian), [`arxiv`](/docs/user-guide/skills/bundled/research/research-arxiv) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# LLM Wiki Карпат

Создавайте и поддерживайте постоянную комплексную базу знаний в виде взаимосвязанных файлов уценки.
На основе [шаблона LLM Wiki Андрея Карпати] (https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).

В отличие от традиционного RAG (который заново открывает знания с нуля для каждого запроса), вики
единожды собирает знания и поддерживает их в актуальном состоянии. Перекрестные ссылки уже есть.
Противоречия уже отмечены. Синтез отражает все поступившее в организм.

**Разделение труда.** Человек курирует источники и направляет анализ. Агент
обобщает, делает перекрестные ссылки, файлы и поддерживает последовательность.

## Когда этот навык активируется

Используйте этот навык, когда пользователь:
- Просит создать, построить или запустить вики или базу знаний.
- Просит принять, добавить или обработать источник в их вики.
- Задает вопрос, и по настроенному пути присутствует существующая вики.
- Просит проверить, проверить или проверить работоспособность их вики.
- Ссылается на свою вики, базу знаний или «заметки» в контексте исследования.

## Местоположение вики

**Местоположение:** устанавливается с помощью переменной среды `WIKI_PATH` (например, в `${HERMES_HOME:-~/.hermes}/.env`).

Если не установлено, по умолчанию используется `~/wiki`.

```bash
WIKI="${WIKI_PATH:-$HOME/wiki}"
```

Вики — это просто каталог файлов уценки — откройте его в Obsidian, VS Code или
любой редактор. Никакой базы данных, никаких специальных инструментов не требуется.

## Архитектура: три слоя

<!-- ascii-guard-ignore -->
```
wiki/
├── SCHEMA.md           # Conventions, structure rules, domain config
├── index.md            # Sectioned content catalog with one-line summaries
├── log.md              # Chronological action log (append-only, rotated yearly)
├── raw/                # Layer 1: Immutable source material
│   ├── articles/       # Web articles, clippings
│   ├── papers/         # PDFs, arxiv papers
│   ├── transcripts/    # Meeting notes, interviews
│   └── assets/         # Images, diagrams referenced by sources
├── entities/           # Layer 2: Entity pages (people, orgs, products, models)
├── concepts/           # Layer 2: Concept/topic pages
├── comparisons/        # Layer 2: Side-by-side analyses
└── queries/            # Layer 2: Filed query results worth keeping
```
<!-- ascii-guard-ignore-end -->

**Уровень 1 – необработанные источники:** неизменяемый. Агент читает их, но никогда не изменяет.
**Уровень 2 – Wiki:** файлы уценки, принадлежащие агенту. Созданы, обновлены и
перекрестные ссылки агента.
**Уровень 3 — Схема:** `SCHEMA.md` определяет структуру, соглашения и таксономию тегов.

## Возобновление существующей вики (КРИТИЧНО — делайте это каждый сеанс)

Если у пользователя уже есть вики, **всегда ориентируйтесь, прежде чем что-либо делать**:

① **Прочитайте `SCHEMA.md`** — разберитесь с доменом, соглашениями и таксономией тегов.
② **Читать `index.md`** — узнать, какие страницы существуют, и их краткое содержание.
③ **Сканировать последние `log.md`** — прочитайте последние 20–30 записей, чтобы понять недавнюю активность.

```bash
WIKI="${WIKI_PATH:-$HOME/wiki}"
# Orientation reads at session start
read_file "$WIKI/SCHEMA.md"
read_file "$WIKI/index.md"
read_file "$WIKI/log.md" offset=<last 30 lines>
```

Только после ознакомления следует принимать, запрашивать или анализировать. Это предотвращает:
- Создание дубликатов страниц для уже существующих объектов.
- Отсутствуют перекрестные ссылки на существующий контент.
- Противоречие соглашениям схемы.
- Повторение уже зарегистрированной работы

Для больших вики (более 100 страниц) также запустите быстрый `search_files` по теме.
под рукой, прежде чем создавать что-то новое.

## Инициализация новой вики

Когда пользователь просит создать или запустить вики:

1. Определите путь к вики (из `$WIKI_PATH` env var или спросите пользователя; по умолчанию `~/wiki`)
2. Создайте структуру каталогов, указанную выше.
3. Спросите пользователя, какой домен охватывает вики — будьте конкретны.
4. Напишите `SCHEMA.md`, настроенный для домена (см. шаблон ниже).
5. Напишите начальный `index.md` с разделенным заголовком.
6. Напишите начальный `log.md` с записью создания.
7. Подтвердите, что вики готова, и предложите первые источники для загрузки.

### Шаблон SCHEMA.md

Адаптируйтесь к домену пользователя. Схема ограничивает поведение агента и обеспечивает согласованность:

```markdown
# Wiki Schema

## Domain
[What this wiki covers — e.g., "AI/ML research", "personal health", "startup intelligence"]

## Conventions
- File names: lowercase, hyphens, no spaces (e.g., `transformer-architecture.md`)
- Every wiki page starts with YAML frontmatter (see below)
- Use `[[wikilinks]]` to link between pages (minimum 2 outbound links per page)
- When updating a page, always bump the `updated` date
- Every new page must be added to `index.md` under the correct section
- Every action must be appended to `log.md`
- **Provenance markers:** On pages that synthesize 3+ sources, append `^[raw/articles/source-file.md]`
  at the end of paragraphs whose claims come from a specific source. This lets a reader trace each
  claim back without re-reading the whole raw file. Optional on single-source pages where the
  `sources:` frontmatter is enough.

## Frontmatter
  ```yaml
  ---
  title: Название страницы
  создано: ГГГГ-ММ-ДД
  обновлено: ГГГГ-ММ-ДД
  тип: сущность | концепция | сравнение | запрос | резюме
  теги: [из таксономии ниже]
  источники: [raw/articles/имя-источника.md]
  # Дополнительные сигналы качества:
  уверенность: высокая | средний | низкий # насколько хорошо обоснованы утверждения
  оспаривается: true # устанавливается, когда на странице есть неразрешенные противоречия
  противоречия: [other-page-slug] # страницы, с которыми конфликтует эта страница
  ---
  ```

`confidence` and `contested` are optional but recommended for opinion-heavy or fast-moving
topics. Lint surfaces `contested: true` and `confidence: low` pages for review so weak claims
don't silently harden into accepted wiki fact.

### raw/ Frontmatter

Raw sources ALSO get a small frontmatter block so re-ingests can detect drift:

```yaml
---
source_url: https://example.com/article # исходный URL, если применимо.
загружено: ГГГГ-ММ-ДД
sha256: &lt;шестнадцатеричный дайджест необработанного содержимого под заголовком>
---
```

The `sha256:` lets a future re-ingest of the same URL skip processing when content is unchanged,
and flag drift when it has changed. Compute over the body only (everything after the closing
`---`), not the frontmatter itself.

## Tag Taxonomy
[Define 10-20 top-level tags for the domain. Add new tags here BEFORE using them.]

Example for AI/ML:
- Models: model, architecture, benchmark, training
- People/Orgs: person, company, lab, open-source
- Techniques: optimization, fine-tuning, inference, alignment, data
- Meta: comparison, timeline, controversy, prediction

Rule: every tag on a page must appear in this taxonomy. If a new tag is needed,
add it here first, then use it. This prevents tag sprawl.

## Page Thresholds
- **Create a page** when an entity/concept appears in 2+ sources OR is central to one source
- **Add to existing page** when a source mentions something already covered
- **DON'T create a page** for passing mentions, minor details, or things outside the domain
- **Split a page** when it exceeds ~200 lines — break into sub-topics with cross-links
- **Archive a page** when its content is fully superseded — move to `_archive/`, remove from index

## Entity Pages
One page per notable entity. Include:
- Overview / what it is
- Key facts and dates
- Relationships to other entities ([[wikilinks]])
- Source references

## Concept Pages
One page per concept or topic. Include:
- Definition / explanation
- Current state of knowledge
- Open questions or debates
- Related concepts ([[wikilinks]])

## Comparison Pages
Side-by-side analyses. Include:
- What is being compared and why
- Dimensions of comparison (table format preferred)
- Verdict or synthesis
- Sources

## Update Policy
When new information conflicts with existing content:
1. Check the dates — newer sources generally supersede older ones
2. If genuinely contradictory, note both positions with dates and sources
3. Mark the contradiction in frontmatter: `contradictions: [page-name]`
4. Flag for user review in the lint report
```

### Шаблон index.md

Индекс разбит по типам. Каждая запись представляет собой одну строку: викиссылка + резюме.

```markdown
# Wiki Index

> Content catalog. Every wiki page listed under its type with a one-line summary.
> Read this first to find relevant pages for any query.
> Last updated: YYYY-MM-DD | Total pages: N

## Entities
<!-- Alphabetical within section -->

## Concepts

## Comparisons

## Queries
```

**Правило масштабирования:** если в каком-либо разделе содержится более 50 записей, разделите его на подразделы.
по первой букве или поддомену. Когда индекс превышает общее количество записей 200, создайте
`_meta/topic-map.md`, который группирует страницы по темам для более быстрой навигации.

### Шаблон log.md

```markdown
# Wiki Log

> Chronological record of all wiki actions. Append-only.
> Format: `## [YYYY-MM-DD] action | subject`
> Actions: ingest, update, query, lint, create, archive, delete
> When this file exceeds 500 entries, rotate: rename to log-YYYY.md, start fresh.

## [YYYY-MM-DD] create | Wiki initialized
- Domain: [domain]
- Structure created with SCHEMA.md, index.md, log.md
```

## Основные операции

### 1. Загрузка

Когда пользователь предоставит источник (URL, файл, вставку), интегрируйте его в вики:

① **Захват исходного источника:**
   – URL → используйте `web_extract`, чтобы получить уценку, сохраните в `raw/articles/`.
   - PDF → используйте `web_extract` (работает с PDF-файлами), сохраните в `raw/papers/`.
   - Вставленный текст → сохранить в соответствующий подкаталог `raw/`.
   - Назовите файл описательно: `raw/articles/karpathy-llm-wiki-2026.md`.
   - **Добавьте необработанную вступительную часть** (`source_url`, `ingested`, `sha256` тела).
     При повторном получении того же URL-адреса: повторно вычислите sha256, сравните с сохраненным значением —
     пропустить, если они идентичны, отметить смещение и обновить, если они разные. Это достаточно дешево, чтобы
     делать при каждой повторной загрузке и перехватывать тихие изменения источника.

② **Обсудите с пользователем выводы** — что интересно, что важно для
   домен. (Пропустите это в контексте автоматизации/cron — действуйте напрямую.)

③ **Проверьте, что уже существует** — найдите index.md и используйте `search_files`, чтобы найти
   существующие страницы для упомянутых сущностей/концепций. В этом разница между
   растущая вики и куча дубликатов.

④ **Написание или обновление вики-страниц:**
   – **Новые объекты/концепции.** Создавайте страницы, только если они соответствуют пороговым значениям страницы.
     в SCHEMA.md (2+ упоминания источника или центральное в одном источнике)
   - **Существующие страницы:** Добавляйте новую информацию, обновляйте факты, увеличивайте дату `updated`.
     Если новая информация противоречит существующему контенту, следуйте Политике обновления.
   – **Перекрестная ссылка.** Каждая новая или обновленная страница должна ссылаться как минимум на две другие страницы.
     страницы через `[[wikilinks]]`. Проверьте, есть ли на существующих страницах обратные ссылки.
   - **Теги:** Используйте только теги из таксономии в SCHEMA.md.
   - **Происхождение:** На страницах, объединяющих более 3 источников, добавьте `^[raw/articles/source.md]`.
     маркеры абзацев, утверждения которых относятся к конкретному источнику.
   – **Доверие.** Для претензий с большим количеством мнений, быстро меняющихся или из одного источника установите
     `confidence: medium` или `low` во вступительной части. Не отмечайте `high`, если только
     утверждение хорошо поддерживается во многих источниках.

⑤ **Обновить навигацию:**
   – Добавляйте новые страницы в `index.md` в правильный раздел в алфавитном порядке.
   - Обновление счетчика «Всего страниц» и даты «Последнего обновления» в заголовке индекса.
   - Добавить к `log.md`: `## [YYYY-MM-DD] ingest | Source Title`
   - Перечислите каждый файл, созданный или обновленный, в записи журнала.

⑥ **Сообщить об изменениях** — список всех файлов, созданных или обновленных для пользователя.

Один источник может запускать обновления на 5–15 вики-страницах. Это нормально
и желаемое — это эффект сложения.

### 2. Запрос

Когда пользователь задает вопрос о домене вики:

① **Прочитайте `index.md`**, чтобы найти соответствующие страницы.
② **Для вики, содержащих более 100 страниц**, также `search_files` во всех файлах `.md`.
   для ключевых слов — индекс сам по себе может пропустить релевантный контент.
③ **Прочитайте соответствующие страницы**, используя `read_file`.
④ **Синтезируйте ответ** на основе накопленных знаний. Цитировать вики-страницы
   вы взяли из: «На основе [[страницы-a]] и [[страницы-b]...»
⑤ **Сохраняйте ценные ответы обратно** — если ответ представляет собой существенное сравнение,
   глубокое погружение или новый синтез, создайте страницу в `queries/` или `comparisons/`.
   Не сохраняйте в архиве тривиальные запросы — только ответы, повторное получение которых было бы болезненно.
⑥ **Обновить log.md**, указав запрос и информацию о том, был ли он сохранен.

### 3. Ворс

Когда пользователь просит выполнить анализ, проверку работоспособности или аудит вики:

① **Сиротские страницы:** Найдите страницы без входящих `[[wikilinks]]` с других страниц.
```python
# Use execute_code for this — programmatic scan across all wiki pages
import os, re
from collections import defaultdict
wiki = "<WIKI_PATH>"
# Scan all .md files in entities/, concepts/, comparisons/, queries/
# Extract all [[wikilinks]] — build inbound link map
# Pages with zero inbound links are orphans
```

② **Неработающие вики-ссылки:** Найдите `[[links]]`, указывающие на несуществующие страницы.

③ **Полнота индекса:** Каждая вики-страница должна появляться в `index.md`. Сравнить
   файловая система против записей индекса.

④ **Проверка Frontmatter:** На каждой вики-странице должны быть все обязательные поля.
   (название, создано, обновлено, тип, теги, источники). Теги должны быть в таксономии.

⑤ **Устаревший контент:** страницы, дата `updated` которых более чем на 90 дней старше самой
   недавний источник, в котором упоминаются те же лица.

⑥ **Противоречия:** Страницы одной темы с противоречивыми утверждениями. Ищите
   страницы, которые имеют общие теги/сущности, но содержат разные факты. Открыть все страницы
   с заголовком `contested: true` или `contradictions:` для просмотра пользователем.

⑦ **Признаки качества:** укажите страницы с `confidence: low` и все страницы, на которых цитируются
   только один источник, но не имеет набора доверительных полей — это кандидаты
   либо за поиск подтверждения, либо за понижение в должности до `confidence: medium`.

⑧ **Смещение источника:** Для каждого файла в `raw/` с вступительной частью `sha256:` пересчитать
   хэш и флаг не совпадают. Несоответствия указывают на то, что необработанный файл был отредактирован.
   (не должно произойти — raw/ является неизменяемым) или получено с URL-адреса, который с тех пор
   изменился. Несерьезная ошибка, но о ней стоит сообщить.

⑨ **Размер страницы:** Отмечайте страницы длиной более 200 строк — кандидаты на разделение.

⑩ **Аудит тегов:** Перечислите все используемые теги, отметьте те, которые не входят в таксономию SCHEMA.md.

⑪ **Ротация журналов:** Если log.md превышает 500 записей, выполните ротацию.

⑫ **Отчет о результатах** с указанием конкретных путей к файлам и предлагаемых действий, сгруппированных по
   серьезность (неработающие ссылки > потерянные ссылки > смещение исходного кода > оспариваемые страницы > устаревший контент > проблемы со стилем).

⑬ **Добавить в log.md:** `## [YYYY-MM-DD] lint | N issues found`

## Работа с Вики

### Поиск

```bash
# Find pages by content
search_files "transformer" path="$WIKI" file_glob="*.md"

# Find pages by filename
search_files "*.md" target="files" path="$WIKI"

# Find pages by tag
search_files "tags:.*alignment" path="$WIKI" file_glob="*.md"

# Recent activity
read_file "$WIKI/log.md" offset=<last 20 lines>
```

### Массовая загрузка

При одновременном приеме нескольких источников группируйте обновления:
1. Сначала прочтите все источники
2. Определите все сущности и концепции во всех источниках.
3. Проверьте существующие страницы на наличие всех (один проход поиска, а не N)
4. Создавайте/обновляйте страницы за один проход (избегайте повторных обновлений).
5. Обновите index.md один раз в конце.
6. Напишите одну запись в журнале, охватывающую партию.

### Архивирование

Когда контент полностью заменяется или изменяется область действия домена:
1. Создайте каталог `_archive/`, если он не существует.
2. Переместите страницу в `_archive/` по исходному пути (например, `_archive/entities/old-page.md`).
3. Удалить из `index.md`
4. Обновите все страницы, на которые есть ссылки — замените викиссылку обычным текстом + «(в архиве)».
5. Зарегистрируйте действие архива.

### Обсидиановая интеграция

Каталог wiki «из коробки» работает как хранилище Obsidian:
- `[[wikilinks]]` отображается в виде кликабельных ссылок.
- Представление графика визуализирует сеть знаний.
- Интерфейс YAML обеспечивает запросы Dataview.
- В папке `raw/assets/` хранятся изображения, на которые есть ссылка через `![[image.png]]`.

Для достижения наилучших результатов:
- Установите папку вложений Obsidian на `raw/assets/`.
- Включите «Вики-ссылки» в настройках Obsidian (обычно включено по умолчанию).
- Установите плагин Dataview для таких запросов, как `TABLE tags FROM "entities" WHERE contains(tags, "company")`.

Если вы используете навык «Обсидиан» вместе с этим, установите `OBSIDIAN_VAULT_PATH` в значение
тот же каталог, что и путь к вики.

### Obsidian Headless (серверы и безголовые машины)

На компьютерах без дисплея используйте `obsidian-headless` вместо настольного приложения.
Он синхронизирует хранилища через Obsidian Sync без графического интерфейса — идеально подходит для агентов, работающих на
серверы, которые пишут в вики, в то время как настольный компьютер Obsidian читает ее на другом устройстве.

**Настройка:**
```bash
# Requires Node.js 22+
npm install -g obsidian-headless

# Login (requires Obsidian account with Sync subscription)
ob login --email <email> --password '<password>'

# Create a remote vault for the wiki
ob sync-create-remote --name "LLM Wiki"

# Connect the wiki directory to the vault
cd ~/wiki
ob sync-setup --vault "<vault-id>"

# Initial sync
ob sync

# Continuous sync (foreground — use systemd for background)
ob sync --continuous
```

**Непрерывная фоновая синхронизация через systemd:**
```ini
# ~/.config/systemd/user/obsidian-wiki-sync.service
[Unit]
Description=Obsidian LLM Wiki Sync
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/path/to/ob sync --continuous
WorkingDirectory=%h/wiki
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now obsidian-wiki-sync
# Enable linger so sync survives logout:
sudo loginctl enable-linger $USER
```

Это позволяет агенту писать в `~/wiki` на сервере, пока вы просматриваете тот же
хранилище в Obsidian на вашем ноутбуке/телефоне — изменения появляются в течение нескольких секунд.

## Подводные камни

- **Никогда не изменяйте файлы в `raw/`** — исходники неизменяемы. Исправления идут на вики-страницах.
- **Всегда сначала ориентируйтесь** — читайте СХЕМУ + индекс + недавний журнал перед любой операцией в новом сеансе.
  Пропуск этого параметра приведет к появлению дубликатов и пропущенным перекрестным ссылкам.
- **Всегда обновляйте index.md и log.md** — пропуск этого пункта приведет к ухудшению качества вики. Это
  навигационная магистраль.
- **Не создавайте страницы для мимолетных упоминаний** — следуйте пороговым значениям страниц в SCHEMA.md. Имя
  появление одного раза в сноске не гарантирует наличие страницы объекта.
- **Не создавайте страницы без перекрестных ссылок** — изолированные страницы невидимы. Каждая страница должна
  ссылка как минимум на 2 другие страницы.
- **Требуется Frontmatter** — он обеспечивает поиск, фильтрацию и обнаружение устаревших данных.
- **Теги должны исходить из таксономии** — теги произвольной формы превращаются в шум. Добавить новые теги в SCHEMA.md
  сначала, а потом использовать их.
- **Сохраняйте возможность сканирования страниц** — вики-страница должна быть доступна для чтения в течение 30 секунд. Разделить страницы на части
  200 строк. Переместите подробный анализ на специальные страницы с подробным описанием.
- **Спросить перед массовым обновлением** — если вставка затронет более 10 существующих страниц, подтвердите.
  область действия с пользователем в первую очередь.
- **Ротация журнала** – когда log.md превышает 500 записей, переименуйте его в `log-YYYY.md` и начните заново.
  Агент должен проверить размер журнала во время проверки.
– **Явно обрабатывайте противоречия** — не перезаписывайте молча. Обратите внимание на обе претензии с датами,
  отметить во обложке, пометить для просмотра пользователем.

## Сопутствующие инструменты

[llm-wiki-compiler](https://github.com/atomicmemory/llm-wiki-compiler) — это интерфейс командной строки Node.js, который
компилирует источники в концептуальную вики с тем же вдохновением, что и Карпаты. Он совместим с Obsidian,
поэтому пользователи, которым нужен запланированный конвейер компиляции/управляемый CLI, могут указать его в том же хранилище, что и здесь.
навык сохраняется. Компромиссы: он владеет генерацией страниц (заменяет мнение агента о странице).
создание) и настроен для небольших корпусов. Используйте этот навык, если вам нужно постоянное курирование агента;
используйте llmwiki, если хотите пакетную компиляцию исходного каталога.