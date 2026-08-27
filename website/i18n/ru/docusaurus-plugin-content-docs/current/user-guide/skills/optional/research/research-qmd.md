---
title: Qmd — гибридный локальный поиск по заметкам, документам и стенограммам.
sidebar_label: Qmd
description: Гибридный локальный поиск по заметкам, документам и расшифровкам
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# QMD

Гибридный локальный поиск по заметкам, документам и расшифровкам.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/research/qmd` |
| Путь | `optional-skills/research/qmd` |
| Версия | `1.0.0` |
| Автор | Гермес Агент + Текниум |
| Лицензия | Массачусетский технологический институт |
| Платформы | Макос, Linux |
| Теги | `Search`, `Knowledge-Base`, `RAG`, `Notes`, `MCP`, `Local-AI` |
| Сопутствующие навыки | [`obsidian`](/docs/user-guide/skills/bundled/note-king/note-cover-obsidian), [`hermes-agent`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-hermes-agent), [`arxiv`](/docs/user-guide/skills/bundled/research/research-arxiv) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# QMD — документы разметки запроса

Локальная поисковая система на устройстве для личных баз знаний. Уценка индексов
заметки, стенограммы встреч, документацию и любые текстовые файлы, а затем
обеспечивает гибридный поиск, сочетающий сопоставление ключевых слов, семантическое понимание и
Изменение рейтинга на основе LLM — все работает локально, без облачных зависимостей.

Создано [Тоби Лютке](https://github.com/tobi/qmd). Лицензия MIT.

## Когда использовать

- Пользователь просит выполнить поиск в своих заметках, документах, базе знаний или стенограммах собраний.
- Пользователь хочет найти что-то в большой коллекции уцененных/текстовых файлов.
- Пользователь хочет семантический поиск («найти заметки о концепции X»), а не просто grep по ключевым словам.
- Пользователь уже настроил коллекции qmd и хочет запросить их.
- Пользователь просит настроить местную базу знаний или систему поиска документов.
- Ключевые слова: «поиск в моих заметках», «найти в моих документах», «база знаний», «qmd».

## Предварительные условия

### Node.js >= 22 (обязательно)

```bash
# Check version
node --version  # must be >= 22

# macOS — install or upgrade via Homebrew
brew install node@22

# Linux — use NodeSource or nvm
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
# or with nvm:
nvm install 22 && nvm use 22
```

### SQLite с поддержкой расширений (только для macOS)

В системе macOS SQLite отсутствует загрузка расширений. Установить через Homebrew:

```bash
brew install sqlite
```

### Установить qmd

```bash
npm install -g @tobilu/qmd
# or with Bun:
bun install -g @tobilu/qmd
```

При первом запуске автоматически загружаются 3 локальные модели GGUF (всего около 2 ГБ):

| Модель | Цель | Размер |
|-------|---------|------|
| встраиваниеgemma-300M-Q8_0 | Векторные вложения | ~300 МБ |
| qwen3-reranker-0.6b-q8_0 | Изменение рейтинга результатов | ~640 МБ |
| qmd-запрос-расширение-1.7B | Расширение запроса | ~1,1 ГБ |

### Проверка установки

```bash
qmd --version
qmd status
```

## Краткий справочник

| Команда | Что он делает | Скорость |
|---------|-------------|-------|
| `qmd search "query"` | Поиск по ключевым словам BM25 (без моделей) | ~0,2 с |
| `qmd vsearch "query"` | Семантический векторный поиск (1 модель) | ~3 с |
| `qmd query "query"` | Гибрид + реранкинг (все 3 модели) | ~2-3 секунды тепло, ~19 секунд холод |
| `qmd get <docid>` | Получить полное содержимое документа | мгновенный |
| `qmd multi-get "glob"` | Получить несколько файлов | мгновенный |
| `qmd collection add <path> --name <n>` | Добавить каталог как коллекцию | мгновенный |
| `qmd context add <path> "description"` | Добавьте метаданные контекста для улучшения поиска | мгновенный |
| `qmd embed` | Создание/обновление векторных вложений | варьируется |
| `qmd status` | Показать информацию о состоянии индекса и коллекциях | мгновенный |
| `qmd mcp` | Запустить сервер MCP (stdio) | стойкий |
| `qmd mcp --http --daemon` | Запустить MCP-сервер (HTTP, теплые модели) | стойкий |

## Рабочий процесс настройки

### 1. Добавить коллекции

Укажите qmd каталоги, содержащие ваши документы:

```bash
# Add a notes directory
qmd collection add ~/notes --name notes

# Add project docs
qmd collection add ~/projects/myproject/docs --name project-docs

# Add meeting transcripts
qmd collection add ~/meetings --name meetings

# List all collections
qmd collection list
```

### 2. Добавьте описания контекста

Метаданные контекста помогают поисковой системе понять, что представляет собой каждая коллекция.
содержит. Это значительно улучшает качество поиска:

```bash
qmd context add qmd://notes "Personal notes, ideas, and journal entries"
qmd context add qmd://project-docs "Technical documentation for the main project"
qmd context add qmd://meetings "Meeting transcripts and action items from team syncs"
```

### 3. Генерация вложений

```bash
qmd embed
```

При этом обрабатываются все документы во всех коллекциях и создаются векторные
вложения. Повторный запуск после добавления новых документов или коллекций.

### 4. Проверьте

```bash
qmd status   # shows index health, collection stats, model info
```

## Шаблоны поиска

### Быстрый поиск по ключевым словам (BM25)

Лучше всего подходит для: точных терминов, идентификаторов кода, имен, известных фраз.
Никакие модели не загружены — результаты почти мгновенные.

```bash
qmd search "authentication middleware"
qmd search "handleError async"
```

### Поиск семантического вектора

Лучше всего подходит для: вопросов на естественном языке, концептуальных запросов.
Загружает модель внедрения (первый запрос ~3 секунды).

```bash
qmd vsearch "how does the rate limiter handle burst traffic"
qmd vsearch "ideas for improving onboarding flow"
```

### Гибридный поиск с изменением рейтинга (наилучшее качество)

Лучше всего подходит для: важных запросов, где качество имеет решающее значение.
Использует все 3 модели — расширение запроса, параллельный вектор BM25+, переранжирование.

```bash
qmd query "what decisions were made about the database migration"
```

### Структурированные многорежимные запросы

Объедините различные типы поиска в одном запросе для большей точности:

```bash
# BM25 for exact term + vector for concept
qmd query $'lex: rate limiter\nvec: how does throttling work under load'

# With query expansion
qmd query $'expand: database migration plan\nlex: "schema change"'
```

### Синтаксис запроса (режим lex/BM25)

| Синтаксис | Эффект | Пример |
|--------|--------|---------|
| `term` | Префикс совпадения | `perf` соответствует «производительности» |
| `"phrase"` | Точная фраза | `"rate limiter"` |
| `-term` | Исключить термин | `performance -sports` |

### HyDE (гипотетические внедрения документов)

Для сложных тем напишите, как вы ожидаете получить ответ:

```bash
qmd query $'hyde: The migration plan involves three phases. First, we add the new columns without dropping the old ones. Then we backfill data. Finally we cut over and remove legacy columns.'
```

### Выбор коллекций

```bash
qmd search "query" --collection notes
qmd query "query" --collection project-docs
```

### Выходные форматы

```bash
qmd search "query" --json        # JSON output (best for parsing)
qmd search "query" --limit 5     # Limit results
qmd get "#abc123"                # Get by document ID
qmd get "path/to/file.md"       # Get by file path
qmd get "file.md:50" -l 100     # Get specific line range
qmd multi-get "journals/*.md" --json  # Batch retrieve by glob
```

## Интеграция MCP (рекомендуется)

qmd предоставляет сервер MCP, который предоставляет инструменты поиска непосредственно
Агент Hermes через собственный клиент MCP. Это предпочтительный
интеграция — после настройки агент автоматически получает инструменты qmd
без необходимости загружать этот навык.

### Вариант A: режим Stdio (простой)

Добавьте в `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  qmd:
    command: "qmd"
    args: ["mcp"]
    timeout: 30
    connect_timeout: 45
```

При этом регистрируются инструменты: `mcp_qmd_search`, `mcp_qmd_vsearch`,
`mcp_qmd_deep_search`, `mcp_qmd_get`, `mcp_qmd_status`.

**Компромисс:** модели загружаются при первом поиске (холодный старт ~19 секунд),
затем оставайтесь в тепле во время сеанса. Приемлемо для эпизодического использования.

### Вариант B: Режим HTTP-демона (быстрый, рекомендуется для интенсивного использования)

Отдельно запустите демон qmd — он сохраняет модели в памяти:

```bash
# Start daemon (persists across agent restarts)
qmd mcp --http --daemon

# Runs on http://localhost:8181 by default
```

Затем настройте агент Hermes для подключения через HTTP:

```yaml
mcp_servers:
  qmd:
    url: "http://localhost:8181/mcp"
    timeout: 30
```

**Компромисс:** во время работы используется около 2 ГБ ОЗУ, но каждый запрос выполняется быстро.
(~2–3 с). Лучше всего подходит для пользователей, которые часто ищут.

### Поддержание работы демона

#### macOS (запущен)

```bash
cat > ~/Library/LaunchAgents/com.qmd.daemon.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.qmd.daemon</string>
  <key>ProgramArguments</key>
  <array>
    <string>qmd</string>
    <string>mcp</string>
    <string>--http</string>
    <string>--daemon</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/qmd-daemon.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/qmd-daemon.log</string>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.qmd.daemon.plist
```

#### Linux (пользовательская служба systemd)

```bash
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/qmd-daemon.service << 'EOF'
[Unit]
Description=QMD MCP Daemon
After=network.target

[Service]
ExecStart=qmd mcp --http --daemon
Restart=on-failure
RestartSec=10
Environment=PATH=/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now qmd-daemon
systemctl --user status qmd-daemon
```

### Справочник по инструментам MCP

После подключения эти инструменты будут доступны под именем `mcp_qmd_*`:

| Инструмент MCP | Карты | Описание |
|----------|---------|-------------|
| `mcp_qmd_search` | `qmd search` | BM25 поиск по ключевым словам |
| `mcp_qmd_vsearch` | `qmd vsearch` | Семантический векторный поиск |
| `mcp_qmd_deep_search` | `qmd query` | Гибридный поиск + реранжирование |
| `mcp_qmd_get` | `qmd get` | Получить документ по идентификатору или пути |
| `mcp_qmd_status` | `qmd status` | Индекс здоровья и статистика |

Инструменты MCP принимают структурированные запросы JSON для многорежимного поиска:

```json
{
  "searches": [
    {"type": "lex", "query": "authentication middleware"},
    {"type": "vec", "query": "how user login is verified"}
  ],
  "collections": ["project-docs"],
  "limit": 10
}
```

## Использование CLI (без MCP)

Если MCP не настроен, используйте qmd напрямую через терминал:

```
terminal(command="qmd query 'what was decided about the API redesign' --json", timeout=30)
```

Для задач настройки и управления всегда используйте терминал:

```
terminal(command="qmd collection add ~/Documents/notes --name notes")
terminal(command="qmd context add qmd://notes 'Personal research notes and ideas'")
terminal(command="qmd embed")
terminal(command="qmd status")
```

## Как работает поисковый конвейер

Понимание внутреннего устройства помогает выбрать правильный режим поиска:

1. **Расширение запроса**. Тщательно настроенная модель 1.7B генерирует 2 альтернативных варианта.
   запросы. Оригинал получает в 2 раза больше веса в Fusion.
2. **Параллельный поиск** — BM25 (SQLite FTS5) и векторный поиск.
   одновременно по всем вариантам запроса.
3. **RRF Fusion** — взаимное объединение рангов (k=60) объединяет результаты.
   Бонус за высший ранг: №1 получает +0,05, №2-3 получает +0,02.
4. **Реранкинг LLM** — программа qwen3-reranker оценивает 30 лучших кандидатов (0,0–1,0).
5. **Смешивание с учетом позиции** — ранги 1–3: 75 % извлечения / 25 % повторного ранжирования.
   Ранги 4–10: 60/40. Ранг 11+: 40/60 (больше доверяет реранкеру для длинного хвоста).

**Умное группирование.** Документы разбиваются по естественным точкам разрыва (заголовки,
блоки кода, пустые строки), ориентированные на ~900 токенов с перекрытием 15%. Код
блоки никогда не разделяются в середине блока.

## Лучшие практики

1. **Всегда добавляйте контекстные описания** — `qmd context add` резко
   повышает точность поиска. Опишите, что содержит каждая коллекция.
2. **Повторное встраивание после добавления документов** — `qmd embed` необходимо запустить повторно, когда
   в коллекции добавляются новые файлы.
3. **Используйте `qmd search` для скорости** — если вам нужен быстрый поиск по ключевым словам.
   (кодовые идентификаторы, точные названия), BM25 мгновенный и не требует моделей.
4. **Используйте `qmd query` для качества** — когда вопрос концептуальный или
   пользователю нужны наилучшие результаты, используйте гибридный поиск.
5. **Предпочитайте интеграцию MCP** — после настройки агент становится родным.
   инструменты без необходимости каждый раз загружать этот навык.
6. **Режим демона для частых пользователей** — если пользователь ищет в своих
   регулярно пользуйтесь базой знаний, порекомендуйте установку демона HTTP.
7. **Первый запрос в структурированном поиске получает удвоенный вес** — ставьте наибольший
   важный/определенный запрос первым при объединении lex и vec.

## Устранение неполадок

### "Модели загружаются при первом запуске"
Нормальный — qmd автоматически загружает ~2 ГБ моделей GGUF при первом использовании.
Это разовая операция.

### Задержка холодного старта (~19 с)
Это происходит, когда модели не загружаются в память. Решения:
- Используйте режим демона HTTP (`qmd mcp --http --daemon`), чтобы согреться.
- Используйте `qmd search` (только BM25), когда модели не нужны.
- Режим MCP stdio загружает модели при первом поиске, остается теплым во время сеанса.

### macOS: «невозможно загрузить расширение»
Установите доморощенный SQLite: `brew install sqlite`
Затем убедитесь, что он находится в PATH перед системным SQLite.

### "Коллекции не найдены"
Запустите `qmd collection add <path> --name <name>`, чтобы добавить каталоги,
затем `qmd embed`, чтобы проиндексировать их.

### Встраивание переопределения модели (CJK/многоязычный)
Установите переменную среды `QMD_EMBED_MODEL` для неанглоязычного контента:
```bash
export QMD_EMBED_MODEL="your-multilingual-model"
```

## Хранение данных

- **Индекс и векторы:** `~/.cache/qmd/index.sqlite`
- **Модели:** автоматически загружаются в локальный кеш при первом запуске.
- **Нет облачных зависимостей** — все работает локально

## Ссылки

- [GitHub: tobi/qmd](https://github.com/tobi/qmd)
- [Журнал изменений QMD](https://github.com/tobi/qmd/blob/main/CHANGELOG.md)