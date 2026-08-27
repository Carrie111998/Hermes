---
title: Code Wiki — создание вики-документов + диаграмм русалок для любой кодовой базы.
sidebar_label: Code Wiki
description: Создавайте вики-документы + диаграммы Mermaid для любой кодовой базы.
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Код Вики

Создавайте вики-документы + диаграммы Mermaid для любой кодовой базы.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/software-development/code-wiki` |
| Путь | `optional-skills/software-development/code-wiki` |
| Версия | `0.1.0` |
| Автор | Текниум (текниум1), Агент Гермеса |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `Documentation`, `Mermaid`, `Architecture`, `Diagrams`, `Wiki`, `Code-Analysis` |
| Сопутствующие навыки | [`codebase-inspection`](/docs/user-guide/skills/bundled/github/github-codebase-inspection), [`github-repo-management`](/docs/user-guide/skills/bundled/github/github-github-repo-management) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Код Wiki Skill

Создайте полную вики для любой кодовой базы — обзор, архитектуру, подробное описание каждого модуля, класс Mermaid и диаграммы последовательности. Вдохновлен Google CodeWiki, но работает с локальными и частными репозиториями и на любом языке. Использует только существующие инструменты Hermes (`terminal`, `read_file`, `search_files`, `write_file`); ни Docker, ни внешних сервисов, ни дополнительных зависимостей.

Этот навык создает **справочную документацию** (что/как). Он не создает стратегического повествования (почему — это другой навык).

## Когда использовать

- Пользователь говорит: «Задокументируйте эту кодовую базу», «создайте вики», «создайте архитектурные диаграммы».
- Присоединяетесь к незнакомому репозиторию и хотите получить структурированную ссылку.
- Пользователь указывает на URL-адрес GitHub и запрашивает документацию.
- Нужен стабильный артефакт (уценка + Русалка), который отображается на GitHub.

НЕ используйте это для:
- Однофайловая или однофункциональная документация — просто ответьте напрямую
– Ссылка на API для одной конкретной конечной точки — используйте `read_file` и ответьте онлайн.
- Стратегическое повествование «почему это существует» — разные навыки, разные цели.
- Кодовые базы, которые пользователь активно разрабатывает в этом сеансе — просто отвечайте на вопросы по мере их поступления.

## Предварительные условия

- Никаких переменных окружения не требуется.
- `git` в PATH для отслеживания SHA репозитория и удаленного клонирования.
- Необязательно: `pygount` для статистики языковой разбивки (см. навык `codebase-inspection`).

## Как бежать

Вызовите инструмент `terminal` из корня целевого репозитория, затем используйте `read_file` / `search_files` / `write_file` для создания вики. Местоположение вывода по умолчанию — `~/.hermes/wikis/<repo-name>/`. Записывайте в репозиторий (`docs/wiki/`) только тогда, когда пользователь явно запрашивает это.

## Краткий справочник

| Шаг | Действие |
|---|---|
| 1 | Разрешить цель — локальный cwd, заданный путь или `git clone --depth 50 <url>` во временный каталог |
| 2 | Структура сканирования — `ls`, `find -maxdepth 3`, файлы манифеста, README |
| 3 | Выберите 8–10 модулей для документирования |
| 4 | Напишите `README.md` (обзор + карта модуля) |
| 5 | Напишите `architecture.md` с помощью блок-схемы Русалки |
| 6 | Написание документации для каждого модуля в `modules/` |
| 7 | Напишите `diagrams/class-diagram.md` (Диаграмма класса Русалки) |
| 8 | Напишите `diagrams/sequences.md` (Схема последовательности «Русалка», 2–4 рабочих процесса) |
| 9 | Напишите `getting-started.md` |
| 10 | Напишите `api.md`, если применимо, иначе пропустите |
| 11 | Напишите `.codewiki-state.json` |
| 12 | Сообщить о путях пользователю |

## Процедура

### 1. Решите цель

Для URL-адреса GitHub:

```bash
WIKI_TMP=$(mktemp -d)
git clone --depth 50 <url> "$WIKI_TMP/repo"
cd "$WIKI_TMP/repo"
REPO_SHA=$(git rev-parse HEAD)
REPO_NAME=$(basename <url> .git)
```

Для локального пути (или cwd, если он не указан):

```bash
cd <path>
REPO_SHA=$(git rev-parse HEAD 2>/dev/null || echo "uncommitted")
REPO_NAME=$(basename "$PWD")
```

Затем установите выходной каталог:

```bash
OUTPUT_DIR="$HOME/.hermes/wikis/$REPO_NAME"
mkdir -p "$OUTPUT_DIR/modules" "$OUTPUT_DIR/diagrams"
```

### 2. Структура репозитория сканирования

Используйте инструмент `terminal` для работы оболочки и `read_file` для манифестов:

```bash
# Shallow tree first
ls -la

# Deeper tree, noise filtered
find . -type d \
  -not -path '*/\.*' \
  -not -path '*/node_modules*' \
  -not -path '*/venv*' \
  -not -path '*/__pycache__*' \
  -not -path '*/dist*' \
  -not -path '*/build*' \
  -not -path '*/target*' \
  -maxdepth 3 | sort

# Language breakdown (skip if pygount unavailable)
pygount --format=summary \
  --folders-to-skip=".git,node_modules,venv,.venv,__pycache__,.cache,dist,build,target" \
  . 2>/dev/null || true
```

Затем `read_file` соответствующие манифесты (`package.json`, `pyproject.toml`, `setup.py`, `Cargo.toml`, `go.mod`, `pom.xml`, `build.gradle`) и README проекта. Используйте `search_files target='files'`, чтобы найти их, а не угадывать имена.

### 3. Выберите модули для документирования

Ограничьте первоначальный проход **8–10 модулями**. Эвристика по языкам:

- Python: пакеты верхнего уровня (каталоги с `__init__.py`), а также каталоги подсистемы.
- JS/TS: `src/<subdir>`, каталоги рабочей области верхнего уровня.
- Rust: каждый ящик в рабочей области или каталогах `src/<module>` верхнего уровня.
- Go: каждый каталог пакетов верхнего уровня.
- Смешанный/незнакомый: каталоги верхнего уровня, содержащие исходный код (не конфиг, не тесты).

Для очень больших репозиториев расставьте приоритеты по:
1. Количество импортированных из (модуль, импортированный многими, является основным)
2. LOC (более крупные модули обычно требуют отдельной документации)
3. Упоминания в README/документах верхнего уровня.

Сообщите пользователю список модулей перед созданием документации для каждого модуля в больших репозиториях — это дает ему возможность перенаправить.

### 4. Напишите `README.md`

`read_file` реальный файл README проекта, а также 2–3 верхних файла точек входа. Затем `write_file`:

````markdown
# <Project Name>

<One paragraph: what it is and what it's for. Self-contained — don't assume the
reader has the source README.>

## Key Concepts

- **<Concept 1>** — <one line>
- **<Concept 2>** — <one line>

## Entry Points

- [`path/to/main.py`](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/software-development/code-wiki/<link>) — <what runs when you start it>
- [`path/to/cli.py`](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/software-development/code-wiki/<link>) — <CLI surface>

## High-Level Architecture

<2-3 sentences. Detail goes in architecture.md.>

See [architecture.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/software-development/code-wiki/architecture.md).

## Module Map

| Module | Purpose |
|---|---|
| [`<module>`](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/software-development/code-wiki/modules/<module>.md) | <one-line purpose> |

## Getting Started

See [getting-started.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/software-development/code-wiki/getting-started.md).
````

Для целей ссылок в локальном режиме используйте относительные пути. Для клонированных репозиториев используйте `https://github.com/<owner>/<repo>/blob/<sha>/<path>`, чтобы ссылки выдерживали будущие коммиты.

### 5. Напишите `architecture.md`

````markdown
# Architecture

<2-3 paragraphs: shape of the system. What talks to what. Where data enters,
where it exits, where state lives.>

## Components

- **<Component>** — <1-2 sentences>. See [`modules/<module>.md`](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/software-development/code-wiki/modules/<module>.md).

## System Diagram

```русалка
блок-схема ТД
    Пользователь([Пользователь]) --> Вход[Точка входа]
    Запись --> Ядро[Основной движок]
    Ядро --> StorageA[(База данных)]
    Ядро --> ВнешнийAPI{{Внешний API}}
```

## Data Flow

1. **<Step>** — [`<file>`](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/software-development/code-wiki/<link>)
2. **<Step>** — [`<file>`](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/software-development/code-wiki/<link>)

## Key Design Decisions

- <Anything load-bearing the reader should know>
````

**Семантика формы русалки:**
- `[]` = компонент
- `[()]` = база данных/хранилище
- `{{}}` = внешняя служба
- `(())` = точка входа или терминал
- `-->` = вызов синхронизации, `-.->` = асинхронный/событие

Ограничение ~20 узлов на диаграмму. Разделите на поддиаграммы, если они больше.

### 6. Напишите документацию для каждого модуля в `modules/`.

Для каждого выбранного модуля проверьте его макет с помощью `ls`, определите 3–5 наиболее важных файлов (по размеру, по имени `core.py` / `main.py` / `__init__.py`, по частому импорту), затем `read_file` эти файлы (используйте `offset` / `limit`, чтобы читать только то, что вам нужно; предпочитайте `search_files` для определенных символов).

````markdown
# Module: `<module>`

<1-2 sentence purpose.>

## Responsibilities

- <bullet>
- <bullet>

## Key Files

- [`<module>/<file>`](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/software-development/code-wiki/<link>) — <what it does>

## Public API

<Functions/classes/constants other code uses. Group related items. Show
signatures, not full implementations.>

## Internal Structure

<How the module is organized internally. State management.>

## Dependencies

- **Used by:** <other modules>
- **Uses:** <other modules + external libs>

## Notable Patterns / Gotchas

- <Anything non-obvious>
````

### 7. Напишите `diagrams/class-diagram.md`

Выберите 5–10 наиболее важных классов/типов. `read_file` их, а затем напишите:

````markdown
# Class Diagram

## Core Types

```русалка
классДиаграмма
    класс Агент {
        +имя строки
        +список~Инструмент~ инструменты
        +chat(сообщение) строка
    }
    класс Инструмент {
        <<interface>>
        +строка имени
        +execute(args) любой
    }
    Агент -> Инструмент: использует
    Инструмент <|-- TerminalTool
    Инструмент <|-- Веб-инструмент
```

## Notes

<Anything the diagram can't express — lifecycle, threading, etc.>
````

Для языков без классов (Go, C, Rust): используйте диаграмму отношений структур или пропустите class-diagram.md и объясните ее прозой в Architecture.md. Не применяйте силу.

### 8. Напишите `diagrams/sequences.md`

Выберите 2–4 наиболее важных рабочих процесса. Проследите каждый путь вызова через код (прочитайте точку входа, проследите за вызовами функций), затем:

````markdown
# Sequence Diagrams

## Workflow: <Name>

<1 sentence describing what this does and when it runs.>

```русалка
последовательностьдиаграмма
    участник Пользователь
    участник CLI
    участник Агент
    участник LLM
    Пользователь->>CLI: печатает сообщение
    CLI->>Агент: чат (сообщение)
    Агент->>LLM: вызов API
    LLM -->>Агент: ответ + вызовы инструментов
    Агент->>Агент: инструменты выполнения
    Агент -->>CLI: окончательный ответ
```

### Walkthrough

1. **User input** — [`cli.py:HermesCLI.run_session`](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/software-development/code-wiki/<link>)
2. **Message dispatch** — [`run_agent.py:AIAgent.chat`](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/software-development/code-wiki/<link>)
````

Не выдумывайте участников. Каждый блок должен соответствовать реальному компоненту, который читатель может найти в коде.

### 9. Напишите `getting-started.md`

````markdown
# Getting Started

## Prerequisites

<From manifest files + README. Be specific — versions if pinned.>

## Installation

```bash
<exact commands>
```

## First Run

```bash
<minimal command to see the system do something useful>
```

## Common Workflows

### <Workflow 1>
<commands>

## Configuration

- `<config-file>` — <what it controls>
- Env var `<VAR>` — <what it controls>

## Where to Go Next

- Architecture: [architecture.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/software-development/code-wiki/architecture.md)
- Module reference: [README.md#module-map](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/software-development/code-wiki/README.md#module-map)
````

### 10. Напишите `api.md` (пропустите, если не применимо)

Пишите это только в том случае, если проект представляет собой библиотеку или сервер API. Если это:

- Найдите общедоступную поверхность API (экспорт `__init__.py`, спецификации OpenAPI, обработчики маршрутов, экспортированные типы).
- Документируйте каждую общедоступную запись с помощью подписи, параметров, типа возвращаемого значения и однострочного описания.
- Группировать по категориям

### 11. Запись файла состояния

```bash
cat > "$OUTPUT_DIR/.codewiki-state.json" <<EOF
{
  "repo_name": "$REPO_NAME",
  "source_path": "$PWD",
  "source_sha": "$REPO_SHA",
  "generated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "generator": "hermes-agent code-wiki skill v0.1.0",
  "modules_documented": []
}
EOF
```

### 12. Сообщить пользователю

Укажите точно, что было создано и где:

```
Generated wiki at ~/.hermes/wikis/<repo-name>/:
  README.md                   project overview, module map
  architecture.md             system architecture + flowchart
  getting-started.md          setup, first run, workflows
  modules/<N files>           per-module deep-dives
  diagrams/architecture.md    Mermaid flowchart
  diagrams/class-diagram.md   Mermaid class diagram
  diagrams/sequences.md       Mermaid sequence diagrams
```

Если вы клонировали во временный каталог, напомните пользователю, что его можно удалить (`rm -rf "$WIKI_TMP"`) после того, как он просмотрит вики.

## Управление областью действия

Создание полноценной вики для монорепозитория 500K-LOC обходится очень дорого. По умолчанию ограниченная область действия:

- Первоначальное сканирование: максимальная глубина 3 каталога.
- Документы для каждого модуля: ограничение до 10 модулей, если пользователь не расширит объем.
- Чтение пофайлов: предпочитайте `search_files` для символов + `read_file` с `offset`/`limit` вместо полного чтения.
– Пропустить код поставщика (`vendor/`, `third_party/`, сгенерированный код, `_pb2.py`, `.min.js`).

Если пользователь говорит: «Сделайте все полностью», поверьте ему, но сначала прикиньте приблизительную стоимость: «В этом репозитории около 340 исходных файлов, полное покрытие будет дорогим — подтвердите?»

## Повторный запуск/обновление

Если `.codewiki-state.json` уже существует по целевому пути:

- Прочтите его для предыдущего SHA и списка модулей.
- Если исходный SHA совпадает: спросите пользователя, хотят ли они восстановить или пропустить
- Если SHA отличается: предложить перегенерировать только модули с измененными файлами (`git diff --name-only <old-sha> HEAD`)

Полная инкрементная регенерация — это будущее усовершенствование — на данный момент регенерация целиком приемлема.

## Подводные камни

- **Изготовление компонентов.** Каждый узел диаграммы и заявленный вызов функции должны находиться в исходном коде. `read_file`, прежде чем писать. Самый большой недостаток автоматически генерируемых документов — это фальсификация, звучащая правдоподобно.
- **Общая проза AI.** «Этот модуль отвечает за...» не содержит содержания. Скажите, что на самом деле делает модуль, с точки зрения предметной области.
- **Переформулирование кода в прозаическом виде.** Документация модуля, в которой говорится, что «функция `process` обрабатывает вещи, вызывая `process_item` для каждого элемента», хуже, чем просто ссылка на функцию.
- **Русалка > 50 узлов.** Они отображаются неразборчиво. Разделите их.
- **Документируйте тесты, сгенерированный код или предоставленные поставщиками разработки так, как если бы они были кодом продукта.** Пропустите их.
- **Вывод в репозитории без запроса.** Значение по умолчанию: `~/.hermes/wikis/`. Записывайте в репозиторий только тогда, когда пользователь явно запрашивает это.
- **Особые символы русалки должны быть заключены в кавычки:** `A["Tool / Agent"]`, а не `A[Tool / Agent]`. `<br>` для разрывов строк внутри узла.
- **Вложенные границы кода в SKILL.md.** При написании примера уценки, содержащего блок Mermaid, используйте внешние границы с 4 обратными кавычками, чтобы внутренний ` ```mermaid ` doesn't close the outer. (This SKILL.md does it.)
- **classDiagram generics** render as `~T~` (e.g. `List~Tool~`), not `<T>`.
- **GitHub Mermaid theme is fixed** — don't include `%%{init: ...}%%` blocks; they're stripped on render.

## Verification

After writing, verify:

1. **Mermaid blocks balance** — opens equal closes per file:
   ```bash с 3 обратными кавычками
   для f в "$OUTPUT_DIR"/diagrams/*.md "$OUTPUT_DIR"/architecture.md; делать
     opens=$(grep -c '^```mermaid' "$f")
     total=$(grep -c '^```' "$f")
     echo "$f: $открывает блоки русалок, $total всего заборов (ожидаемое количество = открытий*2)"
   сделано
   ```
2. **All expected files exist** —
   ```bash
   ls "$OUTPUT_DIR"/{README.md,architecture.md,getting-started.md,.codewiki-state.json} \
      "$OUTPUT_DIR"/модули/ "$OUTPUT_DIR"/диаграммы/
   ```
3. **Количество модулей соответствует задуманному** — `ls "$OUTPUT_DIR/modules" | wc -l` должно равняться количеству модулей, которое вы установили на шаге 3.
4. **Никаких вымышленных путей** — при проверке работоспособности 2–3 ссылки на источники разрешаются в реальные файлы.