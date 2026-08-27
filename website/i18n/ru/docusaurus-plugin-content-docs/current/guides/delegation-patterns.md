---
sidebar_position: 13
title: Делегирование и параллельная работа
description: Когда и как использовать делегирование субагента — шаблоны для параллельных
  исследований, проверки кода и многофайловой работы
---

# Делегирование и параллельная работа

Hermes может создавать изолированные дочерние агенты для параллельной работы над задачами. Каждый субагент получает свой собственный разговор, сеанс терминала и набор инструментов. Возвращается только окончательная сводка — промежуточные вызовы инструментов никогда не попадают в ваше контекстное окно.

Полную информацию о функциях см. в разделе [Делегирование субагента](/user-guide/features/delegation).

---

## Когда делегировать

**Хорошие кандидаты для делегирования:**
- Подзадачи, требующие большого количества рассуждений (отладка, проверка кода, синтез исследований)
- Задачи, которые наводнят ваш контекст промежуточными данными.
- Параллельные независимые рабочие потоки (исследования A и B одновременно)
- Задачи в свежем контексте, к которым вы хотите, чтобы агент подходил беспристрастно.

**Используйте что-нибудь другое:**
- Вызов одного инструмента → просто используйте инструмент напрямую
- Механическая многоступенчатая работа с логикой между шагами → `execute_code`
- Задачи, требующие взаимодействия с пользователем → субагенты не могут использовать `clarify`.
- Быстрое редактирование файлов → делайте это напрямую
- Надежная длительная работа, которая должна пережить закрытие сеанса или перезапуск процесса → `cronjob` или `terminal(background=True, notify_on_complete=True)`. Делегирование верхнего уровня является асинхронным, но при этом локальным для процесса.

---

## Шаблон: Параллельное исследование

Изучите три темы одновременно и получите структурированное резюме:

```
Research these three topics in parallel:
1. Current state of WebAssembly outside the browser
2. RISC-V server chip adoption in 2025
3. Practical quantum computing applications

Focus on recent developments and key players.
```

За кулисами Гермес использует:

```python
delegate_task(tasks=[
    {
        "goal": "Research WebAssembly outside the browser in 2025",
        "context": "Focus on: runtimes (Wasmtime, Wasmer), cloud/edge use cases, WASI progress"
    },
    {
        "goal": "Research RISC-V server chip adoption",
        "context": "Focus on: server chips shipping, cloud providers adopting, software ecosystem"
    },
    {
        "goal": "Research practical quantum computing applications",
        "context": "Focus on: error correction breakthroughs, real-world use cases, key companies"
    }
])
```

Все три работают одновременно. Каждый субагент самостоятельно выполняет поиск в Интернете и возвращает сводку. Затем родительский агент синтезирует их в связный брифинг.

---

## Шаблон: проверка кода

Делегируйте проверку безопасности субагенту со свежим контекстом, который подходит к коду без предубеждений:

```
Review the authentication module at src/auth/ for security issues.
Check for SQL injection, JWT validation problems, password handling,
and session management. Fix anything you find and run the tests.
```

Ключом является поле `context` — оно должно включать все, что нужно субагенту:

```python
delegate_task(
    goal="Review src/auth/ for security issues and fix any found",
    context="""Project at /home/user/webapp. Python 3.11, Flask, PyJWT, bcrypt.
    Auth files: src/auth/login.py, src/auth/jwt.py, src/auth/middleware.py
    Test command: pytest tests/auth/ -v
    Focus on: SQL injection, JWT validation, password hashing, session management.
    Fix issues found and verify tests pass."""
)
```

:::предупреждение о проблеме контекста
Субагенты **абсолютно ничего** не знают о вашем разговоре. Они начинаются совершенно заново. Если вы делегируете «исправить ошибку, которую мы обсуждали», субагент понятия не имеет, какую ошибку вы имеете в виду. Всегда явно передавайте пути к файлам, сообщения об ошибках, структуру проекта и ограничения.
:::

---

## Шаблон: сравнение альтернатив

Оцените несколько подходов к одной и той же проблеме параллельно, а затем выберите лучший:

```
I need to add full-text search to our Django app. Evaluate three approaches
in parallel:
1. PostgreSQL tsvector (built-in)
2. Elasticsearch via django-elasticsearch-dsl
3. Meilisearch via meilisearch-python

For each: setup complexity, query capabilities, resource requirements,
and maintenance overhead. Compare them and recommend one.
```

Каждый субагент самостоятельно исследует один вариант. Поскольку они изолированы, перекрестного загрязнения не происходит — каждая оценка имеет свои преимущества. Родительский агент получает все три сводки и выполняет сравнение.

---

## Шаблон: многофайловый рефакторинг

Разделите большую задачу рефакторинга на несколько параллельных субагентов, каждый из которых обрабатывает свою часть кодовой базы:

```python
delegate_task(tasks=[
    {
        "goal": "Refactor all API endpoint handlers to use the new response format",
        "context": """Project at /home/user/api-server.
        Files: src/handlers/users.py, src/handlers/auth.py, src/handlers/billing.py
        Old format: return {"data": result, "status": "ok"}
        New format: return APIResponse(data=result, status=200).to_dict()
        Import: from src.responses import APIResponse
        Run tests after: pytest tests/handlers/ -v"""
    },
    {
        "goal": "Update all client SDK methods to handle the new response format",
        "context": """Project at /home/user/api-server.
        Files: sdk/python/client.py, sdk/python/models.py
        Old parsing: result = response.json()["data"]
        New parsing: result = response.json()["data"] (same key, but add status code checking)
        Also update sdk/python/tests/test_client.py"""
    },
    {
        "goal": "Update API documentation to reflect the new response format",
        "context": """Project at /home/user/api-server.
        Docs at: docs/api/. Format: Markdown with code examples.
        Update all response examples from old format to new format.
        Add a 'Response Format' section to docs/api/overview.md explaining the schema."""
    }
])
```

:::совет
Каждый субагент получает свой собственный терминальный сеанс. Они могут работать в одном каталоге проекта, не наступая друг на друга, если они редактируют разные файлы. Если два субагента могут работать с одним и тем же файлом, обработайте этот файл самостоятельно после завершения параллельной работы.
:::

---

## Схема: собрать, затем проанализировать

Используйте `execute_code` для механического сбора данных, а затем поручите выполнение сложного анализа:

```python
# Step 1: Mechanical gathering (execute_code is better here — no reasoning needed)
execute_code("""
from hermes_tools import web_search, web_extract

results = []
for query in ["AI funding Q1 2026", "AI startup acquisitions 2026", "AI IPOs 2026"]:
    r = web_search(query, limit=5)
    for item in r["data"]["web"]:
        results.append({"title": item["title"], "url": item["url"], "desc": item["description"]})

# Extract full content from top 5 most relevant
urls = [r["url"] for r in results[:5]]
content = web_extract(urls)

# Save for the analysis step
import json
with open("/tmp/ai-funding-data.json", "w") as f:
    json.dump({"search_results": results, "extracted": content["results"]}, f)
print(f"Collected {len(results)} results, extracted {len(content['results'])} pages")
""")

# Step 2: Reasoning-heavy analysis (delegation is better here)
delegate_task(
    goal="Analyze AI funding data and write a market report",
    context="""Raw data at /tmp/ai-funding-data.json contains search results and
    extracted web pages about AI funding, acquisitions, and IPOs in Q1 2026.
    Write a structured market report: key deals, trends, notable players,
    and outlook. Focus on deals over $100M."""
)
```

Часто это наиболее эффективный шаблон: `execute_code` дешево обрабатывает более 10 последовательных вызовов инструментов, затем субагент выполняет единственную дорогостоящую задачу рассуждения с чистым контекстом.

---

## Доступ к унаследованному инструменту

Субагенты наследуют включенные наборы инструментов родительского объекта. `delegate_task` не принимает параметр `toolsets`, связанный с моделью, поэтому делегированная работа не может предоставить себе возможности, которых нет у родителя. Настройте родительские инструменты перед началом разговора, когда делегированной задаче требуется доступ через Интернет, через терминал, к файлу или другой доступ. Hermes по-прежнему удаляет инструменты, заблокированные детьми, такие как `clarify`, `memory` и `send_message`; дети оставляют `execute_code` для программного вызова инструментов.

---

## Ограничения

- **3 параллельных задачи по умолчанию**: пакеты по умолчанию включают 3 одновременных подагента (настраивается через `delegation.max_concurrent_children` в config.yaml, нет жесткого потолка, только один этаж)
- **Вложенное делегирование допускается**: конечные субагенты (по умолчанию) не могут вызывать `delegate_task`, `clarify`, `memory` или `execute_code`. Субагенты оркестратора (`role="orchestrator"`) сохраняют `delegate_task` для дальнейшего делегирования, но только тогда, когда значение `delegation.max_spawn_depth` превышает значение по умолчанию, равное 1 (этаж 1, без потолка); остальные три остаются заблокированными. Отключите глобально через `delegation.orchestrator_enabled: false`.

### Настройка параллелизма и глубины

| Конфигурация | По умолчанию | Диапазон | Эффект |
|--------|---------|-------|--------|
| `max_concurrent_children` | 3 | >=1 | Размер параллельного пакета на вызов `delegate_task` |
| `max_spawn_depth` | 1 | >=1 | Сколько уровней делегирования может появиться дальше |

Пример: запуск 30 параллельных рабочих процессов с вложенными субагентами:

```yaml
delegation:
  max_concurrent_children: 30
  max_spawn_depth: 2
```

- **Отдельные терминалы** — каждый субагент получает собственную терминальную сессию с отдельным рабочим каталогом и состоянием.
- **Нет истории разговоров** — субагенты видят только `goal` и `context`, которые родительский агент передает при вызове `delegate_task`.
- **50 итераций по умолчанию** — установите `max_iterations` ниже для простых задач, чтобы сэкономить средства.
- **Ненадежно** — делегирование верхнего уровня выполняется в фоновом режиме и публикует результаты позже, но остается привязанным к сеансу владения и процессу Hermes. Закрытие сеанса, `/stop`, `/new` или перезапуск процесса могут отменить или остановить незавершенную работу. Используйте `cronjob` или `terminal(background=True, notify_on_complete=True)` для работы, которая должна выдерживать эти границы.

---

## Советы

**Будьте конкретны в целях.** «Исправить ошибку» слишком расплывчато. «Исправьте ошибку TypeError в строке 47 api/handlers.py, где процесс_request() получает None от parse_body()», дает субагенту достаточно возможностей для работы.

**Укажите пути к файлам.** Субагенты не знают структуру вашего проекта. Всегда указывайте абсолютные пути к соответствующим файлам, корень проекта и команду тестирования.

**Используйте делегирование для изоляции контекста.** Иногда вам нужен свежий взгляд. Делегирование заставляет вас четко сформулировать проблему, и субагент подходит к ней без предположений, возникших в ходе вашего разговора.

**Проверьте результаты.** Сводки субагентов — это всего лишь сводки. Если субагент говорит: «Ошибка исправлена, тесты пройдены», проверьте это, запустив тесты самостоятельно или прочитав разницу.

---

*Полную информацию о делегировании — все параметры, интеграцию ACP и расширенную настройку — см. в [Делегирование субагента](/user-guide/features/delegation).*