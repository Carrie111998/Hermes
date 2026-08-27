---
title: Руководство — Ограничьте вывод LLM с помощью грамматик; гарантировать действительный
  JSON
sidebar_label: Guidance
description: Ограничьте вывод LLM с помощью грамматик; гарантировать действительный
  JSON
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Руководство

Ограничьте вывод LLM с помощью грамматик; гарантировать действительный JSON.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/mlops/guidance` |
| Путь | `optional-skills/mlops/guidance` |
| Версия | `1.0.1` |
| Автор | Исследование оркестра |
| Лицензия | Массачусетский технологический институт |
| Зависимости | `guidance`, `transformers` |
| Платформы | Linux, MacOS, Windows |
| Теги | `Prompt Engineering`, `Guidance`, `Constrained Generation`, `Structured Output`, `JSON Validation`, `Grammar`, `Microsoft Research`, `Format Enforcement`, `Multi-Step Workflows` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Руководство: Ограниченное создание LLM

## Когда использовать этот навык

Используйте Руководство, когда вам нужно:
- **Управление синтаксисом вывода LLM** с помощью регулярных выражений или грамматик.
- **Гарантия корректного создания JSON/XML/кода**
- **Уменьшение задержки** по сравнению с традиционными подходами к подсказкам.
– **Применяйте структурированные форматы** (даты, адреса электронной почты, идентификаторы и т. д.).
- **Создавайте многоэтапные рабочие процессы** с помощью потока управления Pythonic.
- **Предотвратите неверный вывод** с помощью грамматических ограничений.

**Звезды GitHub**: более 18 000 | **От**: Исследование Microsoft

## Установка

```bash
# Base installation
pip install guidance

# With specific backends
pip install guidance[transformers]  # Hugging Face models
pip install guidance[llama_cpp]     # llama.cpp models
```

## Быстрый старт

### Базовый пример: структурированная генерация

```python
from guidance import models, gen

# Load model (supports OpenAI, Transformers, llama.cpp)
lm = models.OpenAI("gpt-4")

# Generate with constraints
result = lm + "The capital of France is " + gen("capital", max_tokens=5)

print(result["capital"])  # "Paris"
```

### Формат чата с местной моделью

> **Для поддержки ограничений требуется локальный доступ к журналу.** Regex, `select()` и
> Ограниченная генерация на основе грамматики работает только с локальными серверами
> (`Transformers`, `LlamaCpp`). Серверные части удаленного API (`OpenAI` и Azure
> варианты) поддерживают только неограниченный `gen()` / чат — они не могут применять принудительно
> ограничения на уровне токена. Руководство 0.3.x не имеет класса `models.Anthropic`.

```python
from guidance import models, gen, system, user, assistant

# Local model (supports constrained generation)
lm = models.Transformers("microsoft/Phi-4-mini-instruct")

# Use context managers for chat format
with system():
    lm += "You are a helpful assistant."

with user():
    lm += "What is the capital of France?"

with assistant():
    lm += gen(max_tokens=20)
```

## Основные понятия

### 1. Менеджеры контекста

Guidance использует контекстные менеджеры Pythonic для взаимодействия в стиле чата.

```python
from guidance import system, user, assistant, gen

lm = models.Transformers("microsoft/Phi-4-mini-instruct")

# System message
with system():
    lm += "You are a JSON generation expert."

# User message
with user():
    lm += "Generate a person object with name and age."

# Assistant response
with assistant():
    lm += gen("response", max_tokens=100)

print(lm["response"])
```

**Преимущества:**
- Естественный поток чата
- Четкое разделение ролей.
- Легко читать и поддерживать

### 2. Ограниченная генерация

Руководство гарантирует, что выходные данные соответствуют указанным шаблонам с использованием регулярных выражений или грамматик.

#### Ограничения регулярных выражений

```python
from guidance import models, gen

lm = models.Transformers("microsoft/Phi-4-mini-instruct")

# Constrain to valid email format
lm += "Email: " + gen("email", regex=r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

# Constrain to date format (YYYY-MM-DD)
lm += "Date: " + gen("date", regex=r"\d{4}-\d{2}-\d{2}")

# Constrain to phone number
lm += "Phone: " + gen("phone", regex=r"\d{3}-\d{3}-\d{4}")

print(lm["email"])  # Guaranteed valid email
print(lm["date"])   # Guaranteed YYYY-MM-DD format
```

**Как это работает:**
- Regex преобразовано в грамматику на уровне токена.
- Недействительные токены фильтруются во время генерации.
- Модель может выдавать только совпадающие выходные данные.

#### Ограничения выбора

```python
from guidance import models, gen, select

lm = models.Transformers("microsoft/Phi-4-mini-instruct")

# Constrain to specific choices
lm += "Sentiment: " + select(["positive", "negative", "neutral"], name="sentiment")

# Multiple-choice selection
lm += "Best answer: " + select(
    ["A) Paris", "B) London", "C) Berlin", "D) Madrid"],
    name="answer"
)

print(lm["sentiment"])  # One of: positive, negative, neutral
print(lm["answer"])     # One of: A, B, C, or D
```

### 3. Лечение токеном

Руководство автоматически «исправляет» границы токенов между подсказкой и генерацией.

**Проблема.** Токенизация создает неестественные границы.

```python
# Without token healing
prompt = "The capital of France is "
# Last token: " is "
# First generated token might be " Par" (with leading space)
# Result: "The capital of France is  Paris" (double space!)
```

**Решение:** Руководство создает резервную копию одного токена и восстанавливает его.

```python
from guidance import models, gen

lm = models.Transformers("microsoft/Phi-4-mini-instruct")

# Token healing enabled by default
lm += "The capital of France is " + gen("capital", max_tokens=5)
# Result: "The capital of France is Paris" (correct spacing)
```

**Преимущества:**
- Естественные границы текста
- Никаких проблем с неудобным расстоянием
- Лучшая производительность модели (видит естественные последовательности токенов)

### 4. Генерация на основе грамматики

Определите сложные структуры, составляя грамматические функции. Строка-шаблон
Форма `grammar=` не входит в текущее руководство — создавайте грамматики на основе
компонуемые функции или используйте `guidance.json()` для JSON.

```python
from guidance import models, gen
from guidance import json as gen_json
from pydantic import BaseModel, Field

lm = models.Transformers("microsoft/Phi-4-mini-instruct")

# JSON via a Pydantic schema (guidance.json compiles the schema to a grammar)
class Person(BaseModel):
    name: str = Field(pattern=r"[A-Za-z ]+")
    age: int
    email: str = Field(pattern=r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

lm += gen_json(name="person", schema=Person)

print(lm["person"])  # Guaranteed valid JSON matching the schema

# Or compose grammar functions directly:
grammar = "name=" + gen("name", regex=r"[A-Za-z ]+") + " age=" + gen("age", regex=r"[0-9]+")
lm += grammar
```

**Случаи использования:**
- Сложные структурированные результаты
- Вложенные структуры данных
- Синтаксис языка программирования
- Языки, специфичные для предметной области

### 5. Функции управления

Создавайте шаблоны генерации многократного использования с помощью декоратора `@guidance`.

```python
from guidance import guidance, gen, models

@guidance
def generate_person(lm):
    """Generate a person with name and age."""
    lm += "Name: " + gen("name", max_tokens=20, stop="\n")
    lm += "\nAge: " + gen("age", regex=r"[0-9]+", max_tokens=3)
    return lm

# Use the function
lm = models.Transformers("microsoft/Phi-4-mini-instruct")
lm = generate_person(lm)

print(lm["name"])
print(lm["age"])
```

**Функции с сохранением состояния:**

```python
@guidance(stateless=False)
def react_agent(lm, question, tools, max_rounds=5):
    """ReAct agent with tool use."""
    lm += f"Question: {question}\n\n"

    for i in range(max_rounds):
        # Thought
        lm += f"Thought {i+1}: " + gen("thought", stop="\n")

        # Action
        lm += "\nAction: " + select(list(tools.keys()), name="action")

        # Execute tool
        tool_result = tools[lm["action"]]()
        lm += f"\nObservation: {tool_result}\n\n"

        # Check if done
        lm += "Done? " + select(["Yes", "No"], name="done")
        if lm["done"] == "Yes":
            break

    # Final answer
    lm += "\nFinal Answer: " + gen("answer", max_tokens=100)
    return lm
```

## Конфигурация серверной части

### OpenAI (только удаленно — без ограничений)

> Серверные части удаленного API не могут выполнять ограниченную генерацию (регулярное выражение/выбор/грамматика);
> используйте их только для обычного чата/`gen()`. Для ограничений используйте локальный бэкэнд.

```python
from guidance import models

lm = models.OpenAI(
    model="gpt-4o-mini",
    api_key="your-api-key"  # Or set OPENAI_API_KEY env var
)
```

### Локальные модели (Трансформеры)

```python
from guidance.models import Transformers

lm = Transformers(
    "microsoft/Phi-4-mini-instruct",
    device="cuda"  # Or "cpu"
)
```

### Локальные модели (llama.cpp)

```python
from guidance.models import LlamaCpp

lm = LlamaCpp(
    model_path="/path/to/model.gguf",
    n_ctx=4096,
    n_gpu_layers=35
)
```

## Общие шаблоны

### Шаблон 1: Генерация JSON

```python
from guidance import models, gen, system, user, assistant

lm = models.Transformers("microsoft/Phi-4-mini-instruct")

with system():
    lm += "You generate valid JSON."

with user():
    lm += "Generate a user profile with name, age, and email."

with assistant():
    lm += """{
    "name": """ + gen("name", regex=r'"[A-Za-z ]+"', max_tokens=30) + """,
    "age": """ + gen("age", regex=r"[0-9]+", max_tokens=3) + """,
    "email": """ + gen("email", regex=r'"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"', max_tokens=50) + """
}"""

print(lm)  # Valid JSON guaranteed
```

### Схема 2: Классификация

```python
from guidance import models, gen, select

lm = models.Transformers("microsoft/Phi-4-mini-instruct")

text = "This product is amazing! I love it."

lm += f"Text: {text}\n"
lm += "Sentiment: " + select(["positive", "negative", "neutral"], name="sentiment")
lm += "\nConfidence: " + gen("confidence", regex=r"[0-9]+", max_tokens=3) + "%"

print(f"Sentiment: {lm['sentiment']}")
print(f"Confidence: {lm['confidence']}%")
```

### Модель 3: Многоэтапное рассуждение

```python
from guidance import models, gen, guidance

@guidance
def chain_of_thought(lm, question):
    """Generate answer with step-by-step reasoning."""
    lm += f"Question: {question}\n\n"

    # Generate multiple reasoning steps
    for i in range(3):
        lm += f"Step {i+1}: " + gen(f"step_{i+1}", stop="\n", max_tokens=100) + "\n"

    # Final answer
    lm += "\nTherefore, the answer is: " + gen("answer", max_tokens=50)

    return lm

lm = models.Transformers("microsoft/Phi-4-mini-instruct")
lm = chain_of_thought(lm, "What is 15% of 200?")

print(lm["answer"])
```

### Шаблон 4: Агент ReAct

```python
from guidance import models, gen, select, guidance

@guidance(stateless=False)
def react_agent(lm, question):
    """ReAct agent with tool use."""
    tools = {
        "calculator": lambda expr: eval(expr),
        "search": lambda query: f"Search results for: {query}",
    }

    lm += f"Question: {question}\n\n"

    for round in range(5):
        # Thought
        lm += f"Thought: " + gen("thought", stop="\n") + "\n"

        # Action selection
        lm += "Action: " + select(["calculator", "search", "answer"], name="action")

        if lm["action"] == "answer":
            lm += "\nFinal Answer: " + gen("answer", max_tokens=100)
            break

        # Action input
        lm += "\nAction Input: " + gen("action_input", stop="\n") + "\n"

        # Execute tool
        if lm["action"] in tools:
            result = tools[lm["action"]](lm["action_input"])
            lm += f"Observation: {result}\n\n"

    return lm

lm = models.Transformers("microsoft/Phi-4-mini-instruct")
lm = react_agent(lm, "What is 25 * 4 + 10?")
print(lm["answer"])
```

### Схема 5: Извлечение данных

```python
from guidance import models, gen, guidance

@guidance
def extract_entities(lm, text):
    """Extract structured entities from text."""
    lm += f"Text: {text}\n\n"

    # Extract person
    lm += "Person: " + gen("person", stop="\n", max_tokens=30) + "\n"

    # Extract organization
    lm += "Organization: " + gen("organization", stop="\n", max_tokens=30) + "\n"

    # Extract date
    lm += "Date: " + gen("date", regex=r"\d{4}-\d{2}-\d{2}", max_tokens=10) + "\n"

    # Extract location
    lm += "Location: " + gen("location", stop="\n", max_tokens=30) + "\n"

    return lm

text = "Tim Cook announced at Apple Park on 2024-09-15 in Cupertino."

lm = models.Transformers("microsoft/Phi-4-mini-instruct")
lm = extract_entities(lm, text)

print(f"Person: {lm['person']}")
print(f"Organization: {lm['organization']}")
print(f"Date: {lm['date']}")
print(f"Location: {lm['location']}")
```

## Лучшие практики

### 1. Используйте регулярное выражение для проверки формата

```python
# ✅ Good: Regex ensures valid format
lm += "Email: " + gen("email", regex=r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

# ❌ Bad: Free generation may produce invalid emails
lm += "Email: " + gen("email", max_tokens=50)
```

### 2. Используйте select() для фиксированных категорий

```python
# ✅ Good: Guaranteed valid category
lm += "Status: " + select(["pending", "approved", "rejected"], name="status")

# ❌ Bad: May generate typos or invalid values
lm += "Status: " + gen("status", max_tokens=20)
```

### 3. Используйте исцеление токенов

```python
# Token healing is enabled by default
# No special action needed - just concatenate naturally
lm += "The capital is " + gen("capital")  # Automatic healing
```

### 4. Используйте стоп-последовательности

```python
# ✅ Good: Stop at newline for single-line outputs
lm += "Name: " + gen("name", stop="\n")

# ❌ Bad: May generate multiple lines
lm += "Name: " + gen("name", max_tokens=50)
```

### 5. Создание повторно используемых функций

```python
# ✅ Good: Reusable pattern
@guidance
def generate_person(lm):
    lm += "Name: " + gen("name", stop="\n")
    lm += "\nAge: " + gen("age", regex=r"[0-9]+")
    return lm

# Use multiple times
lm = generate_person(lm)
lm += "\n\n"
lm = generate_person(lm)
```

### 6. Ограничения баланса

```python
# ✅ Good: Reasonable constraints
lm += gen("name", regex=r"[A-Za-z ]+", max_tokens=30)

# ❌ Too strict: May fail or be very slow
lm += gen("name", regex=r"^(John|Jane)$", max_tokens=10)
```

## Сравнение с альтернативами

| Особенность | Руководство | Инструктор | Контуры | ЛМКЛ |
|---------|----------|------------|----------|------|
| Ограничения регулярных выражений | ✅ Да | ❌ Нет | ✅ Да | ✅ Да |
| Поддержка грамматики | ✅ КФГ | ❌ Нет | ✅ КФГ | ✅ КФГ |
| Пидантическая проверка | ❌ Нет | ✅ Да | ✅ Да | ❌ Нет |
| Токен исцеления | ✅ Да | ❌ Нет | ✅ Да | ❌ Нет |
| Локальные модели | ✅ Да | ⚠️ Ограниченная | ✅ Да | ✅ Да |
| Модели API | ✅ Да | ✅ Да | ⚠️ Ограниченная | ✅ Да |
| Питонический синтаксис | ✅ Да | ✅ Да | ✅ Да | ❌ SQL-подобный |
| Кривая обучения | Низкий | Низкий | Средний | Высокий |

**Когда выбирать руководство:**
- Нужны ограничения регулярных выражений/грамматики
- Хотите жетон исцеления
- Построение сложных рабочих процессов с потоком управления
- Использование локальных моделей (Трансформеры, llama.cpp)
- Предпочитаете Pythonic синтаксис

**Когда выбирать альтернативы:**
- Инструктор: необходима проверка Pydantic с автоматической повторной попыткой.
– Краткое описание: требуется проверка схемы JSON.
- LMQL: предпочитаете декларативный синтаксис запроса.

## Характеристики производительности

**Уменьшение задержки:**
- На 30–50 % быстрее, чем традиционные подсказки для ограниченных результатов.
- Лечение токеном уменьшает ненужную регенерацию.
- Грамматические ограничения предотвращают создание недействительных токенов.

**Использование памяти:**
- Минимальные накладные расходы по сравнению с неограниченной генерацией
- Компиляция грамматики кэшируется после первого использования.
- Эффективная фильтрация токенов во время вывода

**Эффективность токена:**
- Предотвращает трату токенов на недействительные выходные данные.
- Нет необходимости в повторных циклах
- Прямой путь к действительным результатам

## Ресурсы

- **Документация**: https://guidance.readthedocs.io.
- **GitHub**: https://github.com/guidance-ai/guidance (более 18 тысяч звезд)
- **Блокноты**: https://github.com/guidance-ai/guidance/tree/main/notebooks
- **Discord**: доступна поддержка сообщества.

## См. также

- `references/constraints.md` — Комплексные шаблоны регулярных выражений и грамматики.
- `references/backends.md` — Конфигурация, специфичная для серверной части
- `references/examples.md` - Готовые к использованию примеры