---
sidebar_position: 5
title: Использование Hermes в качестве библиотеки Python
description: Встраивайте AIAgent в свои собственные скрипты Python, веб-приложения
  или конвейеры автоматизации — интерфейс командной строки не требуется.
---

# Использование Hermes в качестве библиотеки Python

Hermes — это не просто инструмент CLI. Вы можете импортировать `AIAgent` напрямую и использовать его программно в своих собственных скриптах Python, веб-приложениях или конвейерах автоматизации. В этом руководстве показано, как это сделать.

---

## Установка

Клонируйте Hermes и создайте поддерживаемую редактируемую среду разработки:

```bash
git clone https://github.com/NousResearch/hermes-agent.git
cd hermes-agent
uv sync
```

Запустите приложение с помощью `uv run python your_app.py` из этой проверки. Hermes не публикует поддерживаемый дистрибутив колеса или исходного кода для `requirements.txt` установок.

:::совет
При использовании Hermes в качестве библиотеки требуются те же переменные среды, что и CLI. Минимум установите `OPENROUTER_API_KEY` (или `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` при использовании прямого доступа к поставщику).
:::

---

## Базовое использование

Самый простой способ использовать Hermes — метод `chat()` — передать сообщение, получить обратно строку:

```python
from run_agent import AIAgent

agent = AIAgent(
    model="anthropic/claude-sonnet-4.6",
    quiet_mode=True,
)
response = agent.chat("What is the capital of France?")
print(response)
```

`chat()` самостоятельно обрабатывает весь цикл диалога — вызовы инструментов, повторные попытки и т. д. — и возвращает только окончательный текстовый ответ.

:::предупреждение
Всегда устанавливайте `quiet_mode=True` при встраивании Hermes в свой код. Без него агент печатает счетчики CLI, индикаторы прогресса и другие выходные данные терминала, которые будут загромождать выходные данные вашего приложения.
:::

---

## Полный контроль разговора

Для большего контроля над разговором используйте напрямую `run_conversation()`. Он возвращает словарь с полным ответом, историей сообщений и метаданными:

```python
agent = AIAgent(
    model="anthropic/claude-sonnet-4.6",
    quiet_mode=True,
)

result = agent.run_conversation(
    user_message="Search for recent Python 3.13 features",
    task_id="my-task-1",
)

print(result["final_response"])
print(f"Messages exchanged: {len(result['messages'])}")
```

Возвращенный словарь содержит:
- **`final_response`** — окончательный текстовый ответ агента.
- **`messages`** — Полная история сообщений (система, пользователь, помощник, вызовы инструментов)

(Передаваемый вами `task_id` сохраняется в экземпляре агента для изоляции виртуальной машины, но не возвращается в возвращаемый словарь.)

Вы также можете передать собственное системное сообщение, которое переопределяет эфемерное системное приглашение для этого вызова:

```python
result = agent.run_conversation(
    user_message="Explain quicksort",
    system_message="You are a computer science tutor. Use simple analogies.",
)
```

---

## Инструменты настройки

Управляйте тем, к каким наборам инструментов имеет доступ агент, используя `enabled_toolsets` или `disabled_toolsets`:

```python
# Only enable web tools (browsing, search)
agent = AIAgent(
    model="anthropic/claude-sonnet-4.6",
    enabled_toolsets=["web"],
    quiet_mode=True,
)

# Enable everything except terminal access
agent = AIAgent(
    model="anthropic/claude-sonnet-4.6",
    disabled_toolsets=["terminal"],
    quiet_mode=True,
)
```

:::совет
Используйте `enabled_toolsets`, если вам нужен минимальный, заблокированный агент (например, поиск исследовательского бота только в Интернете). Используйте `disabled_toolsets`, если вам нужны большинство возможностей, но вам необходимо ограничить определенные (например, отсутствие доступа к терминалу в общей среде).
:::

---

## Многоходовые разговоры

Сохраняйте состояние разговора на протяжении нескольких ходов, передавая историю сообщений обратно:

```python
agent = AIAgent(
    model="anthropic/claude-sonnet-4.6",
    quiet_mode=True,
)

# First turn
result1 = agent.run_conversation("My name is Alice")
history = result1["messages"]

# Second turn — agent remembers the context
result2 = agent.run_conversation(
    "What's my name?",
    conversation_history=history,
)
print(result2["final_response"])  # "Your name is Alice."
```

Параметр `conversation_history` принимает список `messages` из предыдущего результата. Агент копирует его внутри себя, поэтому исходный список никогда не изменяется.

---

## Сохранение траекторий

Включите сохранение траектории для записи разговоров в формате ShareGPT — полезно для создания обучающих данных или отладки:

```python
agent = AIAgent(
    model="anthropic/claude-sonnet-4.6",
    save_trajectories=True,
    quiet_mode=True,
)

agent.chat("Write a Python function to sort a list")
# Saves to trajectory_samples.jsonl in ShareGPT format
```

Каждый диалог добавляется в виде одной строки JSONL, что упрощает сбор наборов данных в ходе автоматических запусков.

---

## Пользовательские системные подсказки

Используйте `ephemeral_system_prompt`, чтобы настроить пользовательскую системную подсказку, которая определяет поведение агента, но **не** сохраняется в файлах траектории (сохраняя ваши данные обучения в чистоте):

```python
agent = AIAgent(
    model="anthropic/claude-sonnet-4",
    ephemeral_system_prompt="You are a SQL expert. Only answer database questions.",
    quiet_mode=True,
)

response = agent.chat("How do I write a JOIN query?")
print(response)
```

Это идеально подходит для создания специализированных агентов — рецензента кода, автора документации, помощника по SQL — и все они используют одни и те же базовые инструменты.

---

## Пакетная обработка

Для параллельного выполнения множества запросов в Hermes включен `batch_runner.py`. Он управляет параллельными экземплярами `AIAgent` с надлежащей изоляцией ресурсов:

```bash
python batch_runner.py --input prompts.jsonl --output results.jsonl
```

Каждое приглашение имеет собственную `task_id` и изолированную среду. Если вам нужна собственная пакетная логика, вы можете создать свою собственную, используя `AIAgent` напрямую:

```python
import concurrent.futures
from run_agent import AIAgent

prompts = [
    "Explain recursion",
    "What is a hash table?",
    "How does garbage collection work?",
]

def process_prompt(prompt):
    # Create a fresh agent per task for thread safety
    agent = AIAgent(
        model="anthropic/claude-sonnet-4",
        quiet_mode=True,
        skip_memory=True,
    )
    return agent.chat(prompt)

with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
    results = list(executor.map(process_prompt, prompts))

for prompt, result in zip(prompts, results):
    print(f"Q: {prompt}\nA: {result}\n")
```

:::предупреждение
Всегда создавайте **новый экземпляр `AIAgent` для каждого потока или задачи**. Агент поддерживает внутреннее состояние (историю разговоров, сеансы инструментов, счетчики итераций), совместное использование которого не является потокобезопасным.
:::

---

## Примеры интеграции

### Конечная точка FastAPI

```python
from fastapi import FastAPI
from pydantic import BaseModel
from run_agent import AIAgent

app = FastAPI()

class ChatRequest(BaseModel):
    message: str
    model: str = "anthropic/claude-sonnet-4"

@app.post("/chat")
async def chat(request: ChatRequest):
    agent = AIAgent(
        model=request.model,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    response = agent.chat(request.message)
    return {"response": response}
```

### Дискорд-бот

```python
import discord
from run_agent import AIAgent

client = discord.Client(intents=discord.Intents.default())

@client.event
async def on_message(message):
    if message.author == client.user:
        return
    if message.content.startswith("!hermes "):
        query = message.content[8:]
        agent = AIAgent(
            model="anthropic/claude-sonnet-4",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            platform="discord",
        )
        response = agent.chat(query)
        await message.channel.send(response[:2000])

client.run("YOUR_DISCORD_TOKEN")
```

### Шаг конвейера CI/CD

```python
#!/usr/bin/env python3
"""CI step: auto-review a PR diff."""
import subprocess
from run_agent import AIAgent

diff = subprocess.check_output(["git", "diff", "main...HEAD"]).decode()

agent = AIAgent(
    model="anthropic/claude-sonnet-4",
    quiet_mode=True,
    skip_context_files=True,
    skip_memory=True,
    disabled_toolsets=["terminal", "browser"],
)

review = agent.chat(
    f"Review this PR diff for bugs, security issues, and style problems:\n\n{diff}"
)
print(review)
```

---

## Ключевые параметры конструктора

| Параметр | Тип | По умолчанию | Описание |
|-----------|------|---------|-------------|
| `model` | `str` | `""` | Модель в формате OpenRouter (по умолчанию пустая; решается на основе конфигурации Hermes во время выполнения) |
| `quiet_mode` | `bool` | `False` | Подавить вывод CLI |
| `enabled_toolsets` | `List[str]` | `None` | Специальные наборы инструментов в белый список |
| `disabled_toolsets` | `List[str]` | `None` | Определенные наборы инструментов в черный список |
| `save_trajectories` | `bool` | `False` | Сохранять разговоры в JSONL |
| `ephemeral_system_prompt` | `str` | `None` | Пользовательское системное приглашение (не сохраняется в траекториях) |
| `max_iterations` | `int` | `500` | Максимальное количество итераций вызова инструментов за разговор |
| `skip_context_files` | `bool` | `False` | Пропустить загрузку файлов AGENTS.md |
| `skip_memory` | `bool` | `False` | Отключить чтение/запись постоянной памяти |
| `api_key` | `str` | `None` | Ключ API (возврат к переменным окружения) |
| `base_url` | `str` | `None` | URL-адрес конечной точки пользовательского API |
| `platform` | `str` | `None` | Подсказка платформы (`"discord"`, `"telegram"` и т. д.) |

---

## Важные примечания

:::совет
- Установите **`skip_context_files=True`**, если вы не хотите, чтобы файлы `AGENTS.md` из рабочего каталога загружались в системную подсказку.
- Установите **`skip_memory=True`**, чтобы запретить агенту читать или записывать постоянную память — рекомендуется для конечных точек API без отслеживания состояния.
– Параметр `platform` (например, `"discord"`, `"telegram"`) вводит подсказки по форматированию для конкретной платформы, чтобы агент адаптировал свой стиль вывода.
:::

:::предупреждение
- **Потокобезопасность**: создайте один `AIAgent` для каждого потока или задачи. Никогда не делитесь экземпляром между одновременными вызовами.
- **Очистка ресурсов**: агент автоматически очищает ресурсы (сеансы терминала, экземпляры браузера) после завершения разговора. Если вы работаете в долгоживущем процессе, убедитесь, что каждый диалог завершается нормально.
- **Ограничения итераций**: значение по умолчанию `max_iterations=500` является щедрым. Для простых случаев использования вопросов и ответов рассмотрите возможность его снижения (например, `max_iterations=10`), чтобы предотвратить неконтролируемые циклы вызова инструментов и контролировать затраты.
:::