# Формат траектории

Агент Hermes сохраняет траектории разговора в формате JSONL, совместимом с ShareGPT.
для использования в качестве обучающих данных, артефактов отладки и наборов данных для обучения с подкреплением.

Исходные файлы: `agent/trajectory.py`, `run_agent.py` (найти `_save_trajectory`), `batch_runner.py`.


## Соглашение об именовании файлов

Траектории записываются в файлы текущего рабочего каталога:

| Файл | Когда |
|------|------|
| `trajectory_samples.jsonl` | Разговоры, которые завершились успешно (`completed=True`) |
| `failed_trajectories.jsonl` | Разговоры, которые не удались или были прерваны (`completed=False`) |

Пакетный исполнитель (`batch_runner.py`) записывает в пользовательский выходной файл для каждого пакета.
(например, `batch_001_output.jsonl`) с дополнительными полями метаданных.

Вы можете переопределить имя файла с помощью параметра `filename` в `save_trajectory()`.


## Формат записи JSONL

Каждая строка в файле представляет собой автономный объект JSON. Есть два варианта:

### CLI/интерактивный формат (начиная с `_save_trajectory`)

```json
{
  "conversations": [ ... ],
  "timestamp": "2026-03-30T14:22:31.456789",
  "model": "anthropic/claude-sonnet-4.6",
  "completed": true
}
```

### Формат пакетного выполнения (от `batch_runner.py`)

```json
{
  "prompt_index": 42,
  "conversations": [ ... ],
  "metadata": { "prompt_source": "gsm8k", "difficulty": "hard" },
  "completed": true,
  "partial": false,
  "api_calls": 7,
  "toolsets_used": ["code_tools", "file_tools"],
  "tool_stats": {
    "terminal": {"count": 3, "success": 3, "failure": 0},
    "read_file": {"count": 2, "success": 2, "failure": 0},
    "write_file": {"count": 0, "success": 0, "failure": 0}
  },
  "tool_error_counts": {
    "terminal": 0,
    "read_file": 0,
    "write_file": 0
  }
}
```

Словари `tool_stats` и `tool_error_counts` нормализованы и включают в себя
ВСЕ возможные инструменты (начиная с `model_tools.TOOL_TO_TOOLSET_MAP`) с нулевыми значениями по умолчанию,
обеспечение согласованной схемы между записями для загрузки набора данных HuggingFace.


## Массив разговоров (формат ShareGPT)

Массив `conversations` использует соглашения о ролях ShareGPT:

| Роль API | ПоделитьсяGPT `from` |
|----------|-----------------|
| система | `"system"` |
| пользователь | `"human"` |
| помощник | `"gpt"` |
| инструмент | `"tool"` |

### Полный пример

```json
{
  "conversations": [
    {
      "from": "system",
      "value": "You are a function calling AI model. You are provided with function signatures within <tools> </tools> XML tags. You may call one or more functions to assist with the user query. If available tools are not relevant in assisting with user query, just respond in natural conversational language. Don't make assumptions about what values to plug into functions. After calling & executing the functions, you will be provided with function results within <tool_response> </tool_response> XML tags. Here are the available tools:\n<tools>\n[{\"name\": \"terminal\", \"description\": \"Execute shell commands\", \"parameters\": {\"type\": \"object\", \"properties\": {\"command\": {\"type\": \"string\"}}}, \"required\": null}]\n</tools>\nFor each function call return a JSON object, with the following pydantic model json schema for each:\n{'title': 'FunctionCall', 'type': 'object', 'properties': {'name': {'title': 'Name', 'type': 'string'}, 'arguments': {'title': 'Arguments', 'type': 'object'}}, 'required': ['name', 'arguments']}\nEach function call should be enclosed within <tool_call> </tool_call> XML tags.\nExample:\n<tool_call>\n{'name': <function-name>,'arguments': <args-dict>}\n</tool_call>"
    },
    {
      "from": "human",
      "value": "What Python version is installed?"
    },
    {
      "from": "gpt",
      "value": "<think>\nThe user wants to know the Python version. I should run python3 --version.\n</think>\n<tool_call>\n{\"name\": \"terminal\", \"arguments\": {\"command\": \"python3 --version\"}}\n</tool_call>"
    },
    {
      "from": "tool",
      "value": "<tool_response>\n{\"tool_call_id\": \"call_abc123\", \"name\": \"terminal\", \"content\": \"Python 3.11.6\"}\n</tool_response>"
    },
    {
      "from": "gpt",
      "value": "<think>\nGot the version. I can now answer the user.\n</think>\nPython 3.11.6 is installed on this system."
    }
  ],
  "timestamp": "2026-03-30T14:22:31.456789",
  "model": "anthropic/claude-sonnet-4.6",
  "completed": true
}
```


## Правила нормализации

### Разметка содержимого рассуждений

Конвертер траекторий нормализует ВСЕ рассуждения в теги `<think>`, независимо от того,
о том, как модель изначально производила его:

1. **Токены нативного мышления** (поле `msg["reasoning"]` от таких поставщиков, как
   Anthropic, OpenAI o-серия): завернут как `<think>\n{reasoning}\n</think>\n`
   и добавляется перед содержимым.

2. **REASONING_SCRATCHPAD XML** (когда собственное мышление отключено и модель
   причин через XML-код, указанный системой): `<REASONING_SCRATCHPAD>` теги
   преобразован в `<think>` через `convert_scratchpad_to_think()`.

3. **Пустые мыслительные блоки**: на каждом ходу `gpt` гарантированно будет `<think>`.
   блок. Если никаких рассуждений не было сделано, вставляется пустой блок:
   `<think>\n</think>\n` — это обеспечивает единообразный формат обучающих данных.

### Нормализация вызова инструмента

Вызовы инструментов из формата API (с `tool_call_id`, именем функции, аргументами в виде
строка JSON) преобразуются в JSON, завернутый в XML:

```
<tool_call>
{"name": "terminal", "arguments": {"command": "ls -la"}}
</tool_call>
```

— Аргументы анализируются из строк JSON обратно в объекты (без двойного кодирования).
- Если синтаксический анализ JSON не удался (это не должно произойти — проверяется во время разговора),
  пустой `{}` используется с зарегистрированным предупреждением
- Несколько вызовов инструмента за один ход помощника создают несколько блоков `<tool_call>`.
  в одном сообщении `gpt`

### Нормализация отклика инструмента

Все результаты работы инструмента после сообщения помощника группируются в один `tool`.
включите ответы JSON в формате XML:

```
<tool_response>
{"tool_call_id": "call_abc123", "name": "terminal", "content": "output here"}
</tool_response>
```

– Если содержимое инструмента выглядит как JSON (начинается с `{` или `[`), оно анализируется, поэтому
  Поле содержимого содержит объект/массив JSON, а не строку
- Результаты нескольких инструментов объединяются символами новой строки в одном сообщении.
– Имя инструмента по положению сопоставляется с `tool_calls` помощника для родителей.
  массив

### Системное сообщение

Системное сообщение генерируется во время сохранения (не берется из разговора).
Он соответствует шаблону приглашения вызова функции Hermes с:

- Преамбула, объясняющая протокол вызова функций.
- `<tools>` XML-блок, содержащий определения инструментов JSON.
- Ссылка на схему для объектов `FunctionCall`.
- пример `<tool_call>`

Определения инструментов включают `name`, `description`, `parameters` и `required`.
(установлено значение `null`, чтобы соответствовать каноническому формату).


## Загрузка траекторий

Траектории стандартные JSONL — загружаются любым считывателем JSON-строк:

```python
import json

def load_trajectories(path: str):
    """Load trajectory entries from a JSONL file."""
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries

# Filter to successful completions only
successful = [e for e in load_trajectories("trajectory_samples.jsonl")
              if e.get("completed")]

# Extract just the conversations for training
training_data = [e["conversations"] for e in successful]
```

### Загрузка наборов данных HuggingFace

```python
from datasets import load_dataset

ds = load_dataset("json", data_files="trajectory_samples.jsonl")
```

Нормализованная схема `tool_stats` гарантирует, что все записи имеют одинаковые столбцы.
предотвращение ошибок несоответствия схемы Arrow во время загрузки набора данных.


## Управление сохранением траектории

Сохранение траектории — это переключатель `run_agent.py` / уровня библиотеки — `hermes` CLI.
не предоставляет для него ключ конфигурации или флаг:

```bash
python run_agent.py --save_trajectories --query='your question here'
```

Или программно: `AIAgent(..., save_trajectories=True)`/
`initialize_agent(..., save_trajectories=True)`. При включении
Метод `_save_trajectory()` вызывается в конце каждого хода разговора.

Пакетный бегун всегда сохраняет траектории (это его основная цель).

Выборки с нулевыми рассуждениями на всех ходах автоматически отбрасываются
пакетный запуск, чтобы избежать загрязнения обучающих данных необоснованными примерами.