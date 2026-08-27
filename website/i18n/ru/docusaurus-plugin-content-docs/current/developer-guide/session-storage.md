# Хранилище сеансов

Агент Hermes использует базу данных SQLite (`~/.hermes/state.db`) для сохранения сеанса.
метаданные, полная история сообщений и конфигурация модели через CLI и шлюз.
сеансы. Это заменяет более ранний подход к файлам JSONL для каждого сеанса.

Исходный файл: `hermes_state.py`


## Обзор архитектуры

```
~/.hermes/state.db (SQLite, WAL mode)
├── sessions              — Session metadata, token counts, billing
├── messages              — Full message history per session
├── session_model_usage   — Per-model/per-task usage attribution rows
├── messages_fts          — FTS5 virtual table (content + tool_name + tool_calls)
├── messages_fts_trigram  — FTS5 virtual table with trigram tokenizer (CJK / substring search)
├── messages_fts_cjk      — FTS5 virtual table with cjk_unicode61 tokenizer
├── state_meta            — Key/value metadata table
├── gateway_routing       — Gateway routing metadata
├── compression_locks     — Cross-process compression locking
├── async_delegations     — Async delegation bookkeeping
└── schema_version        — Single-row table tracking migration state
```

Ключевые дизайнерские решения:
- **Режим WAL** для одновременного чтения + одного писателя (многоплатформенный шлюз)
- **Виртуальная таблица FTS5** для быстрого текстового поиска по всем сообщениям сеанса.
- **Происхождение сеанса** через цепочки `parent_session_id` (разделение, вызванное сжатием)
- **Теги источника** (`cli`, `telegram`, `discord` и т. д.) для фильтрации платформ.
- Траектории пакетного бегуна и RL здесь НЕ хранятся (отдельные системы)


## Схема SQLite

### Таблица сеансов

Сокращенный — полный текущий список столбцов см. в `SCHEMA_SQL` в `hermes_state.py`.
(который также включает метаданные маршрутизации шлюза, такие как `session_key`, `chat_id`,
`chat_type`, `thread_id`, `display_name`, `origin_json`, `expiry_finalized`,
поля рабочей области `cwd` / `git_branch` / `git_repo_root`, передача обслуживания и
поля ошибки сжатия, `profile_name`, `rewind_count`, `archived` и
`pinned`):

```sql
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    api_call_count INTEGER DEFAULT 0,
    -- ... additional gateway/workspace/handoff/compression columns ...
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_title_unique
    ON sessions(title) WHERE title IS NOT NULL;
```

### Таблица сообщений

Сокращенно — полная схема также включает `effect_disposition`,
`platform_message_id`, `observed`, `active`, `compacted`, `api_content`,
`display_kind` и `display_metadata`:

```sql
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_content TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT,
    codex_message_items TEXT
    -- ... additional display/compaction columns ...
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id, id);
```

Примечания:
- `tool_calls` хранится в виде строки JSON (сериализованный список объектов вызова инструмента)
– `reasoning_details`, `codex_reasoning_items` и `codex_message_items` хранятся как строки JSON.
- `reasoning` хранит необработанный текст обоснования для поставщиков, которые его предоставляют.
- `api_content` — это побочный код с точностью до байта: точная строка содержимого, отправляемая в API для этого сообщения, если она отличается от `content` (эфемерная память/внедрение плагинов, постоянные переопределения). Он сохраняет передаваемые байты для стабильного воспроизведения в кэше запросов — сохраняются в том виде, в котором они были отправлены, за исключением одиночных суррогатов, которые sqlite3 не может связать и которые цикл диалога все равно удаляет из каждой исходящей полезной нагрузки. `NULL` означает, что `content` было отправлено дословно.
- Временные метки представляют собой числа с плавающей запятой эпохи Unix (`time.time()`).

### Полнотекстовый поиск FTS5

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    tool_name,
    tool_calls,
    content='messages',
    content_rowid='id'
);
```

Таблица FTS5 синхронизируется с помощью трех триггеров, которые срабатывают при INSERT, UPDATE и
и УДАЛИТЕ таблицу `messages`. Текущие триггеры запираются на
Маркеры `fts_rebuild_high_water`/`fts_rebuild_progress` в `state_meta` (так что
фоновое восстановление FTS может продолжаться без двойной индексации) и охватывать все три
индексированные столбцы — точный SQL см. в `SCHEMA_SQL` в `hermes_state.py`.


## Версия схемы и миграции

Текущая версия схемы: **23**

Таблица `schema_version` хранит одно целое число. Простые добавления столбцов обрабатываются декларативно с помощью `_reconcile_columns()` (который сравнивает действующие столбцы с `SCHEMA_SQL` и добавляет все недостающие). Цепочка с контролем версий зарезервирована для миграции данных и изменений индекса/FTS, которые не могут быть выражены декларативно:

| Версия | Изменить |
|---------|--------|
| 1 | Исходная схема (сессии, сообщения, FTS5) |
| 2 | Добавить столбец `finish_reason` в сообщения |
| 3 | Добавить столбец `title` в сеансы |
| 4 | Добавить уникальный индекс на `title` (разрешены значения NULL, значения, отличные от NULL, должны быть уникальными) |
| 5 | Добавьте столбцы счетов: `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, `billing_provider`, `billing_base_url`, `billing_mode`, `estimated_cost_usd`, `actual_cost_usd`, `cost_status`, `cost_source`, `pricing_version` |
| 6 | Добавляйте в сообщения столбцы аргументации: `reasoning`, `reasoning_details`, `codex_reasoning_items` |
| 7 | Добавить столбец `reasoning_content` в сообщения |
| 8 | Добавить столбец `api_call_count` в сеансы |
| 9 | Добавьте столбец `codex_message_items` в сообщения для воспроизведения идентификатора сообщения/фазы ответов Кодекса |
| 10 | Добавьте виртуальную таблицу `messages_fts_trigram` (токенизатор триграмм для поиска CJK/подстроки) и заполните существующие строки |
| 11 | Переиндексируйте `messages_fts` и `messages_fts_trigram`, чтобы охватить `tool_name` + `tool_calls`, и переключитесь с внешнего контента на встроенный режим; удалить старые триггеры и заполнить каждую строку сообщения |
| 16 | Пометьте строки субагента-делегата в `model_config` (`$._delegate_from`), чтобы средства выбора сеанса оставались чистыми после того, как родитель удаляет их |
| 18 | Консолидация метаданных шлюза — заполнение `display_name` / `origin_json` / `expiry_finalized` из `sessions.json` |
| 20 | Атрибуция использования по модели — исходные `session_model_usage` строк из исторических совокупных итогов за сеанс |
| 22 | Атрибуция использования по измерению задачи — перестройте `session_model_usage`, чтобы столбец `task` участвовал в PRIMARY KEY |
| 23 | Модернизация хранилища FTS — таблицы FTS с внешним содержимым заменяют копии встроенного режима v11 (переход по желанию для существующих баз данных) |

Версии, не перечисленные выше, представляли собой декларативные добавления столбцов, обработанные `_reconcile_columns()` (только обновление версии, без миграции данных).

Декларативные добавления столбцов используют `ALTER TABLE ADD COLUMN`, завернутый в try/кроме, для обработки случая, когда столбец уже существует (идемпотент). Номер версии увеличивается после каждого успешного блока миграции.


## Обработка конфликтов записи

Несколько процессов Hermes (шлюз + сеансы CLI + агенты рабочего дерева) совместно используют один
`state.db`. Класс `SessionDB` обрабатывает конфликты записи с помощью:

- **Короткий таймаут SQLite** (1 секунда) вместо 30 секунд по умолчанию.
- **Повторная попытка на уровне приложения** со случайным джиттером (20–150 мс, до 15 повторных попыток)
- **НАЧАТЬ НЕМЕДЛЕННО** транзакции, чтобы выявить конфликты блокировок в начале транзакции.
- **Периодические контрольные точки WAL** каждые 50 успешных записей (ПАССИВНЫЙ режим)

Это позволяет избежать «эффекта конвоя», при котором детерминированная внутренняя отсрочка SQLite
заставляет всех конкурирующих авторов повторять попытки через одинаковые промежутки времени.

```
_WRITE_MAX_RETRIES = 15
_WRITE_RETRY_MIN_S = 0.020   # 20ms
_WRITE_RETRY_MAX_S = 0.150   # 150ms
_CHECKPOINT_EVERY_N_WRITES = 50
```


## Общие операции

### Инициализировать

```python
from hermes_state import SessionDB

db = SessionDB()                           # Default: ~/.hermes/state.db
db = SessionDB(db_path=Path("/tmp/test.db"))  # Custom path
```

### Создание сеансов и управление ими

```python
# Create a new session
db.create_session(
    session_id="sess_abc123",
    source="cli",
    model="anthropic/claude-sonnet-4.6",
    user_id="user_1",
    parent_session_id=None,  # or previous session ID for lineage
)

# End a session
db.end_session("sess_abc123", end_reason="user_exit")

# Reopen a session (clear ended_at/end_reason)
db.reopen_session("sess_abc123")
```

### Сохранить сообщения

```python
msg_id = db.append_message(
    session_id="sess_abc123",
    role="assistant",
    content="Here's the answer...",
    tool_calls=[{"id": "call_1", "function": {"name": "terminal", "arguments": "{}"}}],
    token_count=150,
    finish_reason="stop",
    reasoning="Let me think about this...",
)
```

### Получить сообщения

```python
# Raw messages with all metadata
messages = db.get_messages("sess_abc123")

# OpenAI conversation format (for API replay)
conversation = db.get_messages_as_conversation("sess_abc123")
# Returns: [{"role": "user", "content": "..."}, {"role": "assistant", ...}]
```

### Названия сессий

```python
# Set a title (must be unique among non-NULL titles)
db.set_session_title("sess_abc123", "Fix Docker Build")

# Resolve by title (returns most recent in lineage)
session_id = db.resolve_session_by_title("Fix Docker Build")

# Auto-generate next title in lineage
next_title = db.get_next_title_in_lineage("Fix Docker Build")
# Returns: "Fix Docker Build #2"
```


## Полнотекстовый поиск

Метод `search_messages()` поддерживает синтаксис запросов FTS5 с автоматическим
очистка пользовательского ввода.

### Базовый поиск

```python
results = db.search_messages("docker deployment")
```

### Синтаксис запроса FTS5

| Синтаксис | Пример | Значение |
|--------|---------|---------|
| Ключевые слова | `docker deployment` | Оба условия (неявное И) |
| Цитируемая фраза | `"exact phrase"` | Точное фразовое соответствие |
| Логическое ИЛИ | `docker OR kubernetes` | Любой термин |
| Логическое НЕ | `python NOT java` | Исключить термин |
| Префикс | `deploy*` | Префикс совпадения |

### Поиск с фильтром

```python
# Search only CLI sessions
results = db.search_messages("error", source_filter=["cli"])

# Exclude gateway sessions
results = db.search_messages("bug", exclude_sources=["telegram", "discord"])

# Search only user messages
results = db.search_messages("help", role_filter=["user"])
```

### Формат результатов поиска

Каждый результат включает в себя:
- `id`, `session_id`, `role`, `timestamp`
- `snippet` — фрагмент, созданный FTS5, с маркерами `>>>match<<<`.
- `context` — 1 сообщение до и после матча (содержимое сокращено до 200 символов)
- `source`, `model`, `session_started` — из родительского сеанса

Метод `_sanitize_fts5_query()` обрабатывает крайние случаи:
- Удаляет несовпадающие кавычки и специальные символы.
- Заключает термины, написанные через дефис, в кавычки (`chat-send` → `"chat-send"`).
— Удаляет висячие логические операторы (`hello AND` → `hello`).


## Родословная сеанса

Сессии могут образовывать цепочки через `parent_session_id`. Это происходит, когда контекст
сжатие запускает разделение сеанса на шлюзе.

### Запрос: найти происхождение сеанса

```sql
-- Find all ancestors of a session
WITH RECURSIVE lineage AS (
    SELECT * FROM sessions WHERE id = ?
    UNION ALL
    SELECT s.* FROM sessions s
    JOIN lineage l ON s.id = l.parent_session_id
)
SELECT id, title, started_at, parent_session_id FROM lineage;

-- Find all descendants of a session
WITH RECURSIVE descendants AS (
    SELECT * FROM sessions WHERE id = ?
    UNION ALL
    SELECT s.* FROM sessions s
    JOIN descendants d ON s.parent_session_id = d.id
)
SELECT id, title, started_at FROM descendants;
```

### Запрос: последние сеансы с предварительным просмотром

```sql
SELECT s.*,
    COALESCE(
        (SELECT SUBSTR(m.content, 1, 63)
         FROM messages m
         WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
         ORDER BY m.timestamp, m.id LIMIT 1),
        ''
    ) AS preview,
    COALESCE(
        (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
        s.started_at
    ) AS last_active
FROM sessions s
ORDER BY s.started_at DESC
LIMIT 20;
```

### Запрос: статистика использования токенов

```sql
-- Total tokens by model
SELECT model,
       COUNT(*) as session_count,
       SUM(input_tokens) as total_input,
       SUM(output_tokens) as total_output,
       SUM(estimated_cost_usd) as total_cost
FROM sessions
WHERE model IS NOT NULL
GROUP BY model
ORDER BY total_cost DESC;

-- Sessions with highest token usage
SELECT id, title, model, input_tokens + output_tokens AS total_tokens,
       estimated_cost_usd
FROM sessions
ORDER BY total_tokens DESC
LIMIT 10;
```


## Экспорт и очистка

```python
# Export a single session with messages
data = db.export_session("sess_abc123")

# Export all sessions (with messages) as list of dicts
all_data = db.export_all(source="cli")

# Delete old sessions (only ended sessions)
deleted_count = db.prune_sessions(older_than_days=90)
deleted_count = db.prune_sessions(older_than_days=30, source="telegram")

# Clear messages but keep the session record
db.clear_messages("sess_abc123")

# Delete session and all messages
db.delete_session("sess_abc123")
```


## Расположение базы данных

Путь по умолчанию: `~/.hermes/state.db`

Это производное от `hermes_constants.get_hermes_home()`, которое разрешается как
`~/.hermes/` по умолчанию или значение переменной среды `HERMES_HOME`.

Файл базы данных, файл WAL (`state.db-wal`) и файл общей памяти.
(`state.db-shm`) создаются в одном каталоге.