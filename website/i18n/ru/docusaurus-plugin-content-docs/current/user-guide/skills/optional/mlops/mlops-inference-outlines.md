---
title: 'Контуры — Контуры: структурированное генерирование JSON/regex/Pydantic LLM.'
sidebar_label: Outlines
description: 'Краткое описание: структурированное генерирование JSON/regex/Pydantic
  LLM'
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Контуры

Краткое описание: структурированная генерация JSON/regex/Pydantic LLM.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/mlops/outlines` |
| Путь | `optional-skills/mlops/inference/outlines` |
| Версия | `1.0.1` |
| Автор | Исследование оркестра |
| Лицензия | Массачусетский технологический институт |
| Зависимости | `outlines`, `transformers`, `vllm`, `pydantic` |
| Платформы | Linux, MacOS, Windows |
| Теги | `Prompt Engineering`, `Outlines`, `Structured Generation`, `JSON Schema`, `Pydantic`, `Local Models`, `Grammar-Based Generation`, `vLLM`, `Transformers`, `Type Safety` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Контуры: генерация структурированного текста

## Когда использовать этот навык

Используйте Outlines, когда вам нужно:
- **Гарантия допустимой структуры JSON/XML/code** во время генерации.
- **Используйте модели Pydantic** для типобезопасных выходных данных.
- **Поддержка локальных моделей** (Transformers, llama.cpp, vLLM)
- **Максимальная скорость вывода** благодаря структурированной генерации с нулевыми издержками.
- **Автоматическое создание по схемам JSON**.
- **Выборка токенов управления** на уровне грамматики.

**Звезды GitHub**: более 12 000 | **От**: dottxt.ai (ранее .txt)

> **Примечание к API (Outlines 1.x):** Этот навык предназначен для текущей версии API v1.
> Помощники до версии 1.0 (`outlines.models.transformers(...)`,
> `outlines.generate.json/choice/regex/...`) **удалены**. В v1 вы
> создайте модель с помощью `outlines.from_transformers(...)` (или `from_vllm`,
> `from_llamacpp`, `from_openai`), а затем **вызовите модель напрямую** с помощью
> тип вывода: `model(prompt, output_type)`. Возвращаются выходные данные JSON/Pydantic.
> как **строка JSON** — проверьте с помощью `YourModel.model_validate_json(result)`.

## Установка

```bash
# Base installation
pip install outlines

# With specific backends
pip install outlines transformers  # Hugging Face models
pip install outlines llama-cpp-python  # llama.cpp
pip install outlines vllm  # vLLM for high-throughput
```

## Быстрый старт

### Базовый пример: классификация

```python
import outlines
from typing import Literal
from transformers import AutoModelForCausalLM, AutoTokenizer

MODEL_NAME = "microsoft/Phi-3-mini-4k-instruct"

# v1: wrap a Transformers model + tokenizer
model = outlines.from_transformers(
    AutoModelForCausalLM.from_pretrained(MODEL_NAME, device_map="auto"),
    AutoTokenizer.from_pretrained(MODEL_NAME),
)

# Call the model directly with an output type
prompt = "Sentiment of 'This product is amazing!': "
sentiment = model(prompt, Literal["positive", "negative", "neutral"])

print(sentiment)  # "positive" (guaranteed one of these)
```

### С моделями Pydantic

```python
from pydantic import BaseModel
import outlines
from transformers import AutoModelForCausalLM, AutoTokenizer

class User(BaseModel):
    name: str
    age: int
    email: str

MODEL_NAME = "microsoft/Phi-3-mini-4k-instruct"
model = outlines.from_transformers(
    AutoModelForCausalLM.from_pretrained(MODEL_NAME, device_map="auto"),
    AutoTokenizer.from_pretrained(MODEL_NAME),
)

# Generate structured output (returns a JSON string)
prompt = "Extract user: John Doe, 30 years old, john@example.com"
result = model(prompt, User, max_new_tokens=200)

user = User.model_validate_json(result)  # parse into the Pydantic model
print(user.name)   # "John Doe"
print(user.age)    # 30
print(user.email)  # "john@example.com"
```

## Основные понятия

### 1. Ограниченная выборка токенов

Outlines ограничивает генерацию токенов на уровне логита с помощью скомпилированного
автомат, полученный из вашего типа вывода.

**Как это работает:**
1. Преобразуйте тип вывода (JSON/Pydantic/regex/`Literal`) в схему/грамматику.
2. Скомпилируйте грамматику в автомат на уровне токена.
3. Фильтрация недействительных токенов на каждом этапе генерации.
4. Перемотка вперед, когда существует только один действительный токен.

**Преимущества:**
- **Нулевые издержки**: фильтрация происходит на уровне токена.
- **Увеличение скорости**: ускоренная перемотка вперед по детерминированным путям.
- **Гарантированная достоверность**: неверные выходные данные невозможны.

```python
import outlines
from pydantic import BaseModel
from transformers import AutoModelForCausalLM, AutoTokenizer

class Person(BaseModel):
    name: str
    age: int

model = outlines.from_transformers(
    AutoModelForCausalLM.from_pretrained("microsoft/Phi-3-mini-4k-instruct", device_map="auto"),
    AutoTokenizer.from_pretrained("microsoft/Phi-3-mini-4k-instruct"),
)

result = model("Generate person: Alice, 25", Person)
person = Person.model_validate_json(result)
```

### 2. Типы вывода

В версии 1 вы передаете желаемый **тип вывода** непосредственно в качестве второго аргумента.

#### Множественный выбор (`Literal`)

```python
from typing import Literal

sentiment = model("Review: This is great!", Literal["positive", "negative", "neutral"])
# Result: one of the three choices
```

#### JSON через Pydantic

```python
from pydantic import BaseModel

class Product(BaseModel):
    name: str
    price: float
    in_stock: bool

result = model("Extract: iPhone 15, $999, available", Product)
product = Product.model_validate_json(result)  # valid Product instance
```

#### Regex (передать строку регулярного выражения)

```python
# Generate text matching a regex pattern
phone = model("Generate phone number:", r"[0-9]{3}-[0-9]{3}-[0-9]{4}")
# Result: "555-123-4567" (guaranteed to match the pattern)
```

#### Числовые типы

```python
# Pass the Python type directly
age = model("Person's age:", int)      # guaranteed integer
price = model("Product price:", float)  # guaranteed float
```

### 3. Серверная часть модели

Outlines поддерживает несколько локальных серверов и серверов на основе API через фабрики `from_*`.

#### Трансформеры (Обнимающее лицо)

```python
import outlines
from transformers import AutoModelForCausalLM, AutoTokenizer

model = outlines.from_transformers(
    AutoModelForCausalLM.from_pretrained("microsoft/Phi-3-mini-4k-instruct", device_map="auto"),
    AutoTokenizer.from_pretrained("microsoft/Phi-3-mini-4k-instruct"),
)

result = model(prompt, YourModel)
```

#### лама.cpp

```python
import outlines
from llama_cpp import Llama

llm = Llama("./models/llama-3.1-8b-instruct.Q4_K_M.gguf", n_gpu_layers=35, n_ctx=4096)
model = outlines.from_llamacpp(llm)

result = model(prompt, YourModel)
```

#### vLLM (высокая пропускная способность)

```python
import outlines
from vllm import LLM

llm = LLM("meta-llama/Llama-3.1-8B-Instruct", tensor_parallel_size=2)
model = outlines.from_vllm(llm)

result = model(prompt, YourModel)
```

#### OpenAI (JSON с ограничениями на стороне сервера)

```python
import outlines
from openai import OpenAI

client = OpenAI()
model = outlines.from_openai(client, "gpt-4o-mini")

# API backends support JSON-schema style structured output
result = model(prompt, YourModel)
```

### 4. Пидантическая интеграция

Outlines имеет первоклассную поддержку Pydantic с автоматическим переводом схемы.
Генерация возвращает строку JSON; позвоните `model_validate_json`, чтобы получить экземпляр.

#### Базовые модели

```python
from pydantic import BaseModel, Field

class Article(BaseModel):
    title: str = Field(description="Article title")
    author: str = Field(description="Author name")
    word_count: int = Field(description="Number of words", gt=0)
    tags: list[str] = Field(description="List of tags")

result = model("Generate article about AI", Article, max_new_tokens=300)
article = Article.model_validate_json(result)
print(article.title)
print(article.word_count)  # Guaranteed > 0
```

#### Вложенные модели

```python
class Address(BaseModel):
    street: str
    city: str
    country: str

class Person(BaseModel):
    name: str
    age: int
    address: Address  # Nested model

result = model("Generate person in New York", Person)
person = Person.model_validate_json(result)
print(person.address.city)  # "New York"
```

#### Перечисления и литералы

```python
from enum import Enum
from typing import Literal

class Status(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

class Application(BaseModel):
    applicant: str
    status: Status  # Must be one of enum values
    priority: Literal["low", "medium", "high"]  # Must be one of literals

result = model("Generate application", Application)
app = Application.model_validate_json(result)
print(app.status)  # Status.PENDING (or APPROVED/REJECTED)
```

## Общие шаблоны

### Схема 1: извлечение данных

```python
from pydantic import BaseModel
import outlines
from transformers import AutoModelForCausalLM, AutoTokenizer

class CompanyInfo(BaseModel):
    name: str
    founded_year: int
    industry: str
    employees: int

model = outlines.from_transformers(
    AutoModelForCausalLM.from_pretrained("microsoft/Phi-3-mini-4k-instruct", device_map="auto"),
    AutoTokenizer.from_pretrained("microsoft/Phi-3-mini-4k-instruct"),
)

text = """
Apple Inc. was founded in 1976 in the technology industry.
The company employs approximately 164,000 people worldwide.
"""

prompt = f"Extract company information:\n{text}\n\nCompany:"
company = CompanyInfo.model_validate_json(model(prompt, CompanyInfo, max_new_tokens=200))

print(f"Name: {company.name}")
print(f"Founded: {company.founded_year}")
print(f"Industry: {company.industry}")
print(f"Employees: {company.employees}")
```

### Схема 2: Классификация

```python
from typing import Literal
from pydantic import BaseModel

# Binary classification
result = model("Email: Buy now! 50% off!", Literal["spam", "not_spam"])

# Multi-class classification
category = model(
    "Article: Apple announces new iPhone...",
    Literal["technology", "business", "sports", "entertainment"],
)

# With confidence
class Classification(BaseModel):
    label: Literal["positive", "negative", "neutral"]
    confidence: float

out = model("Review: This product is okay, nothing special", Classification)
result = Classification.model_validate_json(out)
```

### Шаблон 3: Структурированные формы

```python
class UserProfile(BaseModel):
    full_name: str
    age: int
    email: str
    phone: str
    country: str
    interests: list[str]

prompt = """
Extract user profile from:
Name: Alice Johnson
Age: 28
Email: alice@example.com
Phone: 555-0123
Country: USA
Interests: hiking, photography, cooking
"""

profile = UserProfile.model_validate_json(model(prompt, UserProfile, max_new_tokens=250))
print(profile.full_name)
print(profile.interests)  # ["hiking", "photography", "cooking"]
```

### Шаблон 4: Извлечение нескольких сущностей

```python
from typing import Literal

class Entity(BaseModel):
    name: str
    type: Literal["PERSON", "ORGANIZATION", "LOCATION"]

class DocumentEntities(BaseModel):
    entities: list[Entity]

text = "Tim Cook met with Satya Nadella at Microsoft headquarters in Redmond."
prompt = f"Extract entities from: {text}"

result = DocumentEntities.model_validate_json(model(prompt, DocumentEntities, max_new_tokens=300))
for entity in result.entities:
    print(f"{entity.name} ({entity.type})")
```

### Шаблон 5: Генерация кода

```python
class PythonFunction(BaseModel):
    function_name: str
    parameters: list[str]
    docstring: str
    body: str

prompt = "Generate a Python function to calculate factorial"
func = PythonFunction.model_validate_json(model(prompt, PythonFunction, max_new_tokens=300))

print(f"def {func.function_name}({', '.join(func.parameters)}):")
print(f'    """{func.docstring}"""')
print(f"    {func.body}")
```

### Шаблон 6: Пакетная обработка

```python
import outlines
from transformers import AutoModelForCausalLM, AutoTokenizer
from pydantic import BaseModel

class Person(BaseModel):
    name: str
    age: int

model = outlines.from_transformers(
    AutoModelForCausalLM.from_pretrained("microsoft/Phi-3-mini-4k-instruct", device_map="auto"),
    AutoTokenizer.from_pretrained("microsoft/Phi-3-mini-4k-instruct"),
)

texts = [
    "John is 30 years old",
    "Alice is 25 years old",
    "Bob is 40 years old",
]

# v1 accepts a list of prompts for batched generation
prompts = [f"Extract from: {t}" for t in texts]
outputs = model(prompts, Person, max_new_tokens=100)
people = [Person.model_validate_json(o) for o in outputs]
for person in people:
    print(f"{person.name}: {person.age}")
```

## Конфигурация серверной части

### Трансформеры

```python
import outlines
from transformers import AutoModelForCausalLM, AutoTokenizer

MODEL_NAME = "microsoft/Phi-3-mini-4k-instruct"

# Basic usage
model = outlines.from_transformers(
    AutoModelForCausalLM.from_pretrained(MODEL_NAME, device_map="auto"),
    AutoTokenizer.from_pretrained(MODEL_NAME),
)

# GPU + dtype configuration is set on the HF model itself
import torch
model = outlines.from_transformers(
    AutoModelForCausalLM.from_pretrained(MODEL_NAME, device_map="cuda", torch_dtype=torch.float16),
    AutoTokenizer.from_pretrained(MODEL_NAME),
)

# Popular models
for name in [
    "meta-llama/Llama-3.1-8B-Instruct",
    "mistralai/Mistral-7B-Instruct-v0.3",
    "Qwen/Qwen2.5-7B-Instruct",
]:
    model = outlines.from_transformers(
        AutoModelForCausalLM.from_pretrained(name, device_map="auto"),
        AutoTokenizer.from_pretrained(name),
    )
```

###лама.cpp

```python
import outlines
from llama_cpp import Llama

# Load GGUF model
llm = Llama(
    "./models/llama-3.1-8b.Q4_K_M.gguf",
    n_ctx=4096,       # Context window
    n_gpu_layers=35,  # GPU layers
    n_threads=8,      # CPU threads
)
model = outlines.from_llamacpp(llm)

# Full GPU offload: set n_gpu_layers=-1 on the Llama object
```

### vLLM (Производство)

```python
import outlines
from vllm import LLM

# Single GPU
model = outlines.from_vllm(LLM("meta-llama/Llama-3.1-8B-Instruct"))

# Multi-GPU
model = outlines.from_vllm(LLM("meta-llama/Llama-3.1-70B-Instruct", tensor_parallel_size=4))

# With quantization
model = outlines.from_vllm(LLM("meta-llama/Llama-3.1-8B-Instruct", quantization="awq"))
```

## Лучшие практики

### 1. Используйте определенные типы

```python
# ✅ Good: Specific types
class Product(BaseModel):
    name: str
    price: float  # Not str
    quantity: int  # Not str
    in_stock: bool  # Not str

# ❌ Bad: Everything as string
class Product(BaseModel):
    name: str
    price: str  # Should be float
    quantity: str  # Should be int
```

### 2. Добавьте ограничения

```python
from pydantic import Field

# ✅ Good: With constraints
class User(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    age: int = Field(ge=0, le=120)
    email: str = Field(pattern=r"^[\w\.-]+@[\w\.-]+\.\w+$")

# ❌ Bad: No constraints
class User(BaseModel):
    name: str
    age: int
    email: str
```

### 3. Используйте перечисления для категорий

```python
# ✅ Good: Enum for fixed set
class Priority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

class Task(BaseModel):
    title: str
    priority: Priority

# ❌ Bad: Free-form string
class Task(BaseModel):
    title: str
    priority: str  # Can be anything
```

### 4. Предоставьте контекст в подсказках

```python
# ✅ Good: Clear context
prompt = """
Extract product information from the following text.
Text: iPhone 15 Pro costs $999 and is currently in stock.
Product:
"""

# ❌ Bad: Minimal context
prompt = "iPhone 15 Pro costs $999 and is currently in stock."
```

### 5. Обработка необязательных полей

```python
from typing import Optional

# ✅ Good: Optional fields for incomplete data
class Article(BaseModel):
    title: str  # Required
    author: Optional[str] = None  # Optional
    date: Optional[str] = None  # Optional
    tags: list[str] = []  # Default empty list

# Can succeed even if author/date missing
```

### 6. Всегда проверяйте вывод JSON

```python
# v1 returns a JSON string for Pydantic/JSON output types.
result = model(prompt, Article)          # str
article = Article.model_validate_json(result)  # Article instance
```

## Сравнение с альтернативами

| Особенность | Контуры | Инструктор | Руководство | ЛМКЛ |
|---------|----------|------------|----------|------|
| Поддержка Пидантика | ✅ Родной | ✅ Родной | ✅ Да | ❌ Нет |
| Схема JSON | ✅ Да | ✅ Да | ✅ Да | ✅ Да |
| Ограничения регулярных выражений | ✅ Да | ❌ Нет | ✅ Да | ✅ Да |
| Локальные модели | ✅ Полный | ⚠️ Ограниченная | ✅ Полный | ✅ Полный |
| Модели API | ✅ Да | ✅ Полный | ✅ Да | ✅ Полный |
| Нулевые накладные расходы | ✅ Да | ❌ Нет | ⚠️ Частичная | ✅ Да |
| Автоматическая повторная попытка | ❌ Нет | ✅ Да | ❌ Нет | ❌ Нет |
| Кривая обучения | Низкий | Низкий | Низкий | Высокий |

**Когда следует выбирать контуры:**
- Использование локальных моделей (Transformers, llama.cpp, vLLM)
- Нужна максимальная скорость вывода
- Хотите поддержку модели Pydantic
- Требуется структурированная генерация с нулевыми накладными расходами.
- Контроль процесса выборки токенов

**Когда выбирать альтернативы:**
- Инструктор: нужны модели API с автоматической повторной попыткой.
- Рекомендации: необходимо восстановление токенов и сложные рабочие процессы.
- LMQL: предпочитаете декларативный синтаксис запроса.

## Характеристики производительности

**Скорость:**
- **Нулевые накладные расходы**: структурированная генерация так же быстро, как и без ограничений.
- **Ускоренная оптимизация**: пропускает детерминированные токены.
- **в 1,2–2 раза быстрее**, чем методы проверки после генерации.

**Память:**
- Автомат компилируется один раз для каждого типа вывода (кэшируется)
- Минимальные накладные расходы во время выполнения
- Эффективность с vLLM для высокой пропускной способности.

**Точность:**
- **100 % действительные выходные данные** (гарантируются ограниченным автоматом)
- Не требуются циклы повторов
- Детерминированная фильтрация токенов

## Ресурсы

- **Документация**: https://dottxt-ai.github.io/outlines/
- **GitHub**: https://github.com/dottxt-ai/outlines (более 12 тысяч звезд)
- **Дискорд**: https://discord.gg/R9DSu34mGd
- **Блог**: https://blog.dottxt.co

## См. также

- `references/json_generation.md` — Комплексные шаблоны JSON и Pydantic.
- `references/backends.md` — Конфигурация, специфичная для серверной части
- `references/examples.md` - Готовые к использованию примеры