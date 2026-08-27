---
title: Разработка на основе субагентов — выполнение планов с помощью субагентов Delegate_task
  (двухэтапная проверка)
sidebar_label: Subagent Driven Development
description: Execute plans via delegate_task subagents (2-stage review)
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Разработка, управляемая субагентами

Выполнять планы через субагенты Delegate_task (двухэтапная проверка).

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/software-development/subagent-driven-development` |
| Путь | `optional-skills/software-development/subagent-driven-development` |
| Версия | `1.1.0` |
| Автор | Агент Гермеса (адаптировано из обры/сверхспособностей) |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `delegation`, `subagent`, `implementation`, `workflow`, `parallel` |
| Сопутствующие навыки | [`plan`](/docs/user-guide/skills/bundled/software-development/software-development-plan), [`requesting-code-review`](/docs/user-guide/skills/bundled/software-development/software-development-requesting-code-review), [`test-driven-development`](/docs/user-guide/skills/bundled/software-development/software-development-test-driven-development) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Разработка на основе субагентов

## Обзор

Выполняйте планы внедрения, направляя новых субагентов для каждой задачи с систематической двухэтапной проверкой.

**Основной принцип:** Свежий субагент для каждой задачи + двухэтапная проверка (спецификация, затем качество) = высокое качество, быстрая итерация.

## Когда использовать

Используйте этот навык, когда:
- У вас есть план внедрения (на основе навыка `plan` или требований пользователя)
- Задачи в основном независимы
- Качество и соответствие спецификациям важны.
- Вам нужен автоматический просмотр между задачами

**против. ручное исполнение:**
- Свежий контекст для каждой задачи (без путаницы из-за накопленного состояния)
- Автоматизированный процесс проверки выявляет проблемы на ранней стадии.
- Постоянный контроль качества всех задач.
- Субагенты могут задавать вопросы перед началом работы.

## Процесс

### 1. Чтение и анализ плана

Прочтите файл плана. Извлеките ВСЕ задачи с их полным текстом и контекстом заранее. Создайте список дел:

```python
# Read the plan
read_file("docs/plans/feature-plan.md")

# Create todo list with all tasks
todo([
    {"id": "task-1", "content": "Create User model with email field", "status": "pending"},
    {"id": "task-2", "content": "Add password hashing utility", "status": "pending"},
    {"id": "task-3", "content": "Create login endpoint", "status": "pending"},
])
```

**Ключ:** Прочитайте план ОДИН РАЗ. Извлеките все. Не заставляйте субагентов читать файл плана — предоставляйте полный текст задачи непосредственно в контексте.

### 2. Рабочий процесс для каждой задачи

Для КАЖДОЙ задачи плана:

#### Шаг 1. Отправка субагента исполнителя

Используйте `delegate_task` с полным контекстом:

```python
delegate_task(
    goal="Implement Task 1: Create User model with email and password_hash fields",
    context="""
    TASK FROM PLAN:
    - Create: src/models/user.py
    - Add User class with email (str) and password_hash (str) fields
    - Use bcrypt for password hashing
    - Include __repr__ for debugging

    FOLLOW TDD:
    1. Write failing test in tests/models/test_user.py
    2. Run: pytest tests/models/test_user.py -v (verify FAIL)
    3. Write minimal implementation
    4. Run: pytest tests/models/test_user.py -v (verify PASS)
    5. Run: pytest tests/ -q (verify no regressions)
    6. Commit: git add -A && git commit -m "feat: add User model with password hashing"

    PROJECT CONTEXT:
    - Python 3.11, Flask app in src/app.py
    - Existing models in src/models/
    - Tests use pytest, run from project root
    - bcrypt already in requirements.txt
    """,
    toolsets=['terminal', 'file']
)
```

#### Шаг 2. Отправка специалиста по проверке соответствия спецификациям

После того, как разработчик завершит работу, проверьте исходную спецификацию:

```python
delegate_task(
    goal="Review if implementation matches the spec from the plan",
    context="""
    ORIGINAL TASK SPEC:
    - Create src/models/user.py with User class
    - Fields: email (str), password_hash (str)
    - Use bcrypt for password hashing
    - Include __repr__

    CHECK:
    - [ ] All requirements from spec implemented?
    - [ ] File paths match spec?
    - [ ] Function signatures match spec?
    - [ ] Behavior matches expected?
    - [ ] Nothing extra added (no scope creep)?

    OUTPUT: PASS or list of specific spec gaps to fix.
    """,
    toolsets=['file']
)
```

**Если обнаружены проблемы со спецификациями:** Исправьте пробелы, а затем повторно запустите проверку спецификаций. Продолжайте только в том случае, если это соответствует спецификациям.

#### Шаг 3. Отправка специалиста по проверке качества кода

После прохождения соответствия спецификациям:

```python
delegate_task(
    goal="Review code quality for Task 1 implementation",
    context="""
    FILES TO REVIEW:
    - src/models/user.py
    - tests/models/test_user.py

    CHECK:
    - [ ] Follows project conventions and style?
    - [ ] Proper error handling?
    - [ ] Clear variable/function names?
    - [ ] Adequate test coverage?
    - [ ] No obvious bugs or missed edge cases?
    - [ ] No security issues?

    OUTPUT FORMAT:
    - Critical Issues: [must fix before proceeding]
    - Important Issues: [should fix]
    - Minor Issues: [optional]
    - Verdict: APPROVED or REQUEST_CHANGES
    """,
    toolsets=['file']
)
```

**Если обнаружены проблемы с качеством:** Исправьте проблемы и повторите проверку. Продолжайте только после одобрения.

#### Шаг 4. Отметьте завершение

```python
todo([{"id": "task-1", "content": "Create User model with email field", "status": "completed"}], merge=True)
```

### 3. Итоговый обзор

После завершения ВСЕХ задач направьте окончательного рецензента интеграции:

```python
delegate_task(
    goal="Review the entire implementation for consistency and integration issues",
    context="""
    All tasks from the plan are complete. Review the full implementation:
    - Do all components work together?
    - Any inconsistencies between tasks?
    - All tests passing?
    - Ready for merge?
    """,
    toolsets=['terminal', 'file']
)
```

### 4. Проверьте и зафиксируйте

```bash
# Run full test suite
pytest tests/ -q

# Review all changes
git diff --stat

# Final commit if needed
git add -A && git commit -m "feat: complete [feature name] implementation"
```

## Детализация задач

**Каждая задача = 2–5 минут целенаправленной работы.**

**Слишком большой:**
- «Внедрить систему аутентификации пользователей»

**Правильный размер:**
- «Создать модель пользователя с полями электронной почты и пароля»
- «Добавить функцию хеширования пароля»
- «Создать конечную точку входа»
- «Добавить генерацию токена JWT»
- «Создать конечную точку регистрации»

## Красные флажки — никогда не делайте этого

- Начать реализацию без плана
- Пропускать проверки (соответствие спецификациям ИЛИ качество кода)
- Приступить к устранению неустраненных критических/важных проблем.
- Отправка нескольких субагентов реализации для задач, касающихся одних и тех же файлов.
- Заставить субагента прочитать файл плана (вместо этого предоставить полный текст в контексте)
- Пропустить контекст настройки сцены (субагент должен понимать, к чему подходит задача)
- Игнорировать вопросы субагента (ответьте, прежде чем разрешить им продолжить)
- Примите «достаточно близко» к соответствию спецификациям
- Пропустить циклы проверки (рецензент обнаружил проблемы → исправления разработчика → повторная проверка).
- Пусть самопроверка исполнителя заменяет фактическую проверку (оба необходимы)
- **Начинайте проверку качества кода до того, как соответствие спецификациям будет пройдено** (неправильный порядок)
- Переходите к следующей задаче, если в любом обзоре есть открытые проблемы.

## Обработка проблем

### Если субагент задает вопросы

- Отвечайте четко и полностью.
- При необходимости укажите дополнительный контекст.
- Не торопите их с реализацией.

### Если рецензент обнаружил проблемы

- Субагент реализации (или новый) исправляет их.
- Рецензент снова проверяет
- Повторяйте до одобрения
- Не пропускайте повторную проверку

### Если субагент не выполнил задачу

- Отправьте новый субагент исправления с конкретными инструкциями о том, что пошло не так.
- Не пытайтесь исправить вручную в сеансе контроллера (загрязнение контекста).

## Примечания по эффективности

**Почему для каждой задачи требуется новый субагент:**
- Предотвращает загрязнение контекста из-за накопленного состояния.
- Каждый субагент получает чистый, сфокусированный контекст.
- Никакой путаницы в коде или рассуждениях предыдущих задач.

**Почему двухэтапная проверка:**
- Обзор спецификации выявляет недоработку/перебор на ранних стадиях
- Проверка качества гарантирует, что реализация хорошо построена.
- Улавливает проблемы до того, как они усугубятся между задачами.

**Компромисс по стоимости:**
- Больше вызовов субагента (исполнитель + 2 рецензента на задачу)
- Но выявляет проблемы на ранней стадии (дешевле, чем отлаживать сложные проблемы позже)

## Интеграция с другими навыками

### С планом

Этот навык ВЫПОЛНЯЕТ планы, созданные навыком `plan`:
1. Требования пользователя → план → план внедрения
2. План реализации → разработка на основе субагента → рабочий код.

### С разработкой через тестирование

Субагенты реализации должны следовать TDD:
1. Сначала напишите неудачный тест
2. Реализуйте минимальный код
3. Проверка прохождения теста
4. Зафиксируйте

Включите инструкции TDD в каждый контекст реализации.

### С запросом проверки кода

Двухэтапный процесс проверки — это проверка кода. Для окончательной проверки интеграции используйте параметры проверки навыка запроса проверки кода.

### С систематической отладкой

Если субагент обнаружил ошибки во время реализации:
1. Следуйте процессу систематической отладки.
2. Прежде чем исправлять, найдите основную причину.
3. Напишите регрессионный тест
4. Возобновить реализацию

## Пример рабочего процесса

```
[Read plan: docs/plans/auth-feature.md]
[Create todo list with 5 tasks]

--- Task 1: Create User model ---
[Dispatch implementer subagent]
  Implementer: "Should email be unique?"
  You: "Yes, email must be unique"
  Implementer: Implemented, 3/3 tests passing, committed.

[Dispatch spec reviewer]
  Spec reviewer: ✅ PASS — all requirements met

[Dispatch quality reviewer]
  Quality reviewer: ✅ APPROVED — clean code, good tests

[Mark Task 1 complete]

--- Task 2: Password hashing ---
[Dispatch implementer subagent]
  Implementer: No questions, implemented, 5/5 tests passing.

[Dispatch spec reviewer]
  Spec reviewer: ❌ Missing: password strength validation (spec says "min 8 chars")

[Implementer fixes]
  Implementer: Added validation, 7/7 tests passing.

[Dispatch spec reviewer again]
  Spec reviewer: ✅ PASS

[Dispatch quality reviewer]
  Quality reviewer: Important: Magic number 8, extract to constant
  Implementer: Extracted MIN_PASSWORD_LENGTH constant
  Quality reviewer: ✅ APPROVED

[Mark Task 2 complete]

... (continue for all tasks)

[After all tasks: dispatch final integration reviewer]
[Run full test suite: all passing]
[Done!]
```

## Помните

```
Fresh subagent per task
Two-stage review every time
Spec compliance FIRST
Code quality SECOND
Never skip reviews
Catch issues early
```

**Качество — это не случайность. Это результат систематического процесса.**

## Дальнейшее чтение (загружайте, если необходимо)

Если оркестровка предполагает значительное использование контекста, длинные циклы проверки или сложные контрольные точки проверки, загрузите эти ссылки для конкретной дисциплины:

- **`references/context-budget-discipline.md`** — Четырехуровневая модель ухудшения контекста (ПИКОВОЕ/ХОРОШЕЕ/Ухудшение/ПЛОХОЕ), правила глубины чтения, которые масштабируются в зависимости от размера окна контекста, а также ранние признаки тихого ухудшения. Загружайте, когда выполнение явно потребует значительного контекста (многоэтапные планы, множество субагентов, крупные артефакты).
- **`references/gates-taxonomy.md`** — четыре канонических типа шлюзов (предварительная проверка, проверка, эскалация, прерывание) с описанием поведения, восстановления и примеров. Загружайте при разработке или проверке любого рабочего процесса, в котором есть контрольные точки проверки — используйте словарь явно, чтобы для каждого шлюза были определены правила входа, поведения при сбое и возобновления.

Обе ссылки адаптированы из gsd-build/get-shit-done (MIT © 2025 Lex Christopherson).