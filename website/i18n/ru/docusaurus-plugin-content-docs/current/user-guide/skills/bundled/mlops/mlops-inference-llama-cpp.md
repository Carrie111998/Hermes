---
title: Llama Cpp — локальный вывод GGUF llama.cpp + обнаружение модели HF Hub
sidebar_label: Llama Cpp
description: llama.cpp локальный вывод GGUF + обнаружение модели HF Hub
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Лама Cpp

llama.cpp локальный вывод GGUF + обнаружение модели HF Hub.

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/mlops/inference/llama-cpp` |
| Версия | `2.1.2` |
| Автор | Исследование оркестра |
| Лицензия | Массачусетский технологический институт |
| Зависимости | `llama-cpp-python>=0.2.0` |
| Платформы | Linux, MacOS, Windows |
| Теги | `llama.cpp`, `GGUF`, `Quantization`, `Hugging Face Hub`, `CPU Inference`, `Apple Silicon`, `Edge Deployment`, `AMD GPUs`, `Intel GPUs`, `NVIDIA`, `URL-first` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# llama.cpp + GGUF

Используйте этот навык для локального вывода GGUF, количественного выбора или обнаружения репозитория Hugging Face для llama.cpp.

## Когда использовать

- Запускайте локальные модели на CPU, Apple Silicon, CUDA, ROCm или графических процессорах Intel.
- Найдите правильный GGUF для конкретного репозитория Hugging Face.
- Создайте команду `llama-server` или `llama-cli` из хаба.
- Найдите в Hub модели, которые уже поддерживают llama.cpp.
– Перечислить доступные `.gguf` файлы и размеры для репозитория.
- Выберите между вариантами Q4/Q5/Q6/IQ для оперативной памяти пользователя или видеопамяти.

## Рабочий процесс обнаружения модели

Предпочитайте рабочие процессы URL, прежде чем запрашивать `hf`, Python или пользовательские скрипты.

1. Найдите репозитории-кандидаты в Хабе:
   - База: `https://huggingface.co/models?apps=llama.cpp&sort=trending`
   - Добавьте `search=<term>` для семейства моделей.
   – Добавьте `num_parameters=min:0,max:24B` или аналогичный, если у пользователя есть ограничения по размеру.
2. Откройте репозиторий с представлением локального приложения llama.cpp:
   - `https://huggingface.co/<repo>?local-app=llama.cpp`
3. Считайте фрагмент локального приложения источником истины, когда он виден:
   - скопируйте точную команду `llama-server` или `llama-cli`.
   - сообщите рекомендуемый количественный показатель точно так, как его показывает HF
4. Прочитайте тот же URL-адрес `?local-app=llama.cpp`, что и текст страницы или HTML, и извлеките раздел под `Hardware compatibility`:
   - предпочитают точные количественные обозначения и размеры общим таблицам
   - сохраняйте метки, специфичные для репозитория, такие как `UD-Q4_K_M` или `IQ4_NL_XL`.
   - если этот раздел не отображается в исходном коде полученной страницы, скажите об этом и вернитесь к API дерева плюс общее руководство по количественному анализу.
5. Запросите API дерева, чтобы подтвердить, что на самом деле существует:
   - `https://huggingface.co/api/models/<repo>/tree/main?recursive=true`
   – сохранять записи, где `type` равен `file`, а `path` заканчивается на `.gguf`.
   - используйте `path` и `size` в качестве источника истины для имен файлов и размеров в байтах.
   - отдельные квантованные контрольные точки из файлов проектора `mmproj-*.gguf` и файлов осколков `BF16/`.
   - используйте `https://huggingface.co/<repo>/tree/main` только как запасной вариант для человека
6. Если фрагмент локального приложения не виден в текстовом виде, восстановите команду из репозитория плюс выбранный квант:
   - сокращенный выбор количества: `llama-server -hf <repo>:<QUANT>`
   - резервный вариант точного файла: `llama-server --hf-repo <repo> --hf-file <filename.gguf>`
7. Предлагайте преобразование из весов Transformers только в том случае, если в репозитории еще не представлены файлы GGUF.

## Быстрый старт

### Установите llama.cpp

```bash
# macOS / Linux (simplest)
brew install llama.cpp
```

```bash
winget install llama.cpp
```

```bash
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp
cmake -B build
cmake --build build --config Release
```

### Запускайте прямо из Hugging Face Hub

```bash
llama-cli -hf bartowski/Llama-3.2-3B-Instruct-GGUF:Q8_0
```

```bash
llama-server -hf bartowski/Llama-3.2-3B-Instruct-GGUF:Q8_0
```

### Запустите точный файл GGUF из хаба

Используйте это, когда API дерева показывает пользовательское именование файлов или точный фрагмент HF отсутствует.

```bash
llama-server \
    --hf-repo microsoft/Phi-3-mini-4k-instruct-gguf \
    --hf-file Phi-3-mini-4k-instruct-q4.gguf \
    -c 4096
```

### Проверка сервера, совместимого с OpenAI

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "user", "content": "Write a limerick about Python exceptions"}
    ]
  }'
```

## Привязки Python (llama-cpp-python)

`pip install llama-cpp-python` (CUDA: `CMAKE_ARGS="-DGGML_CUDA=on" pip install llama-cpp-python --force-reinstall --no-cache-dir`; Металл: `CMAKE_ARGS="-DGGML_METAL=on" ...`).

### Базовое поколение

```python
from llama_cpp import Llama

llm = Llama(
    model_path="./model-q4_k_m.gguf",
    n_ctx=4096,
    n_gpu_layers=35,     # 0 for CPU, 99 to offload everything
    n_threads=8,
)

out = llm("What is machine learning?", max_tokens=256, temperature=0.7)
print(out["choices"][0]["text"])
```

### Чат + стриминг

```python
llm = Llama(
    model_path="./model-q4_k_m.gguf",
    n_ctx=4096,
    n_gpu_layers=35,
    chat_format="llama-3",   # or "chatml", "mistral", etc.
)

resp = llm.create_chat_completion(
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is Python?"},
    ],
    max_tokens=256,
)
print(resp["choices"][0]["message"]["content"])

# Streaming
for chunk in llm("Explain quantum computing:", max_tokens=256, stream=True):
    print(chunk["choices"][0]["text"], end="", flush=True)
```

### Вложения

```python
llm = Llama(model_path="./model-q4_k_m.gguf", embedding=True, n_gpu_layers=35)
vec = llm.embed("This is a test sentence.")
print(f"Embedding dimension: {len(vec)}")
```

Вы также можете загрузить GGUF прямо из хаба:

```python
llm = Llama.from_pretrained(
    repo_id="bartowski/Llama-3.2-3B-Instruct-GGUF",
    filename="*Q4_K_M.gguf",
    n_gpu_layers=35,
)
```

## Выбор количества

Сначала используйте хаб-страницу, а затем общие эвристики.

- Отдавайте предпочтение точному количеству, которое HF помечает как совместимое с профилем оборудования пользователя.
– Общий чат начинается с `Q4_K_M`.
– Для написания кода или технической работы используйте `Q5_K_M` или `Q6_K`, если позволяет память.
– При очень ограниченном бюджете оперативной памяти рассмотрите варианты `Q3_K_M`, `IQ` или `Q2` только в том случае, если пользователь явно отдает приоритет удобству, а не качеству.
– Для мультимодальных репозиториев укажите `mmproj-*.gguf` отдельно. Проектор не является основным файлом модели.
- Не нормализуйте репо-нативные метки. Если на странице указано `UD-Q4_K_M`, сообщите `UD-Q4_K_M`.

## Извлечение доступных GGUF из репозитория

Когда пользователь спрашивает, какие GGUF существуют, верните:

- имя файла
- размер файла
- количественная метка
- будь то основная модель или вспомогательный проектор

Игнорировать, если не требуется:

- ЧИТАТЬ
- Файлы осколков BF16
- пятна imatrix или артефакты калибровки

Используйте API дерева для этого шага:

- `https://huggingface.co/api/models/<repo>/tree/main?recursive=true`

Для репозитория, такого как `unsloth/Qwen3.6-35B-A3B-GGUF`, страница локального приложения может отображать квантовые чипы, такие как `UD-Q4_K_M`, `UD-Q5_K_M`, `UD-Q6_K` и `Q8_0`, а API-интерфейс дерева предоставляет точные пути к файлам, такие как `Qwen3.6-35B-A3B-UD-Q4_K_M.gguf` и `Qwen3.6-35B-A3B-Q8_0.gguf`, с размерами в байтах. Используйте API дерева, чтобы превратить метку квантования в точное имя файла.

## Шаблоны поиска

Используйте эти формы URL напрямую:

```text
https://huggingface.co/models?apps=llama.cpp&sort=trending
https://huggingface.co/models?search=<term>&apps=llama.cpp&sort=trending
https://huggingface.co/models?search=<term>&apps=llama.cpp&num_parameters=min:0,max:24B&sort=trending
https://huggingface.co/<repo>?local-app=llama.cpp
https://huggingface.co/api/models/<repo>/tree/main?recursive=true
https://huggingface.co/<repo>/tree/main
```

## Формат вывода

Отвечая на запросы обнаружения, отдавайте предпочтение компактному структурированному результату, например:

```text
Repo: <repo>
Recommended quant from HF: <label> (<size>)
llama-server: <command>
Other GGUFs:
- <filename> - <size>
- <filename> - <size>
Source URLs:
- <local-app URL>
- <tree API URL>
```

## Ссылки

- **[hub-discovery.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/mlops/inference/llama-cpp/references/hub-discovery.md)** – рабочие процессы Hugging Face, шаблоны поиска, извлечение GGUF и реконструкция команд только для URL-адресов.
- **[advanced-usage.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/mlops/inference/llama-cpp/references/advanced-usage.md)** — спекулятивное декодирование, пакетный вывод, генерация с ограничениями по грамматике, LoRA, несколько графических процессоров, пользовательские сборки, тестовые сценарии
- **[quantization.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/mlops/inference/llama-cpp/references/quantization.md)** — качество количественного анализа, когда использовать Q4/Q5/Q6/IQ, масштабирование размера модели, imatrix
- **[server.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/mlops/inference/llama-cpp/references/server.md)** — запуск сервера напрямую из Hub, конечные точки OpenAI API, развертывание Docker, балансировка нагрузки NGINX, мониторинг
- **[optimization.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/mlops/inference/llama-cpp/references/optimization.md)** — многопоточность ЦП, BLAS, эвристика разгрузки графического процессора, пакетная настройка, тесты
- **[troubleshooting.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/mlops/inference/llama-cpp/references/troubleshooting.md)** — проблемы с установкой/конвертацией/квантизацией/inference/сервером, Apple Silicon, отладка

## Ресурсы

- **GitHub**: https://github.com/ggml-org/llama.cpp
- **Hugging Face GGUF + документация llama.cpp**: https://huggingface.co/docs/hub/gguf-llamacpp
- **Документация по локальным приложениям Hugging Face**: https://huggingface.co/docs/hub/main/local-apps
- **Документация местных агентов Hugging Face**: https://huggingface.co/docs/hub/agents-local
- **Пример страницы локального приложения**: https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF?local-app=llama.cpp.
- **Пример API дерева**: https://huggingface.co/api/models/unsloth/Qwen3.6-35B-A3B-GGUF/tree/main?recursive=true
- **Пример поиска llama.cpp**: https://huggingface.co/models?num_parameters=min:0,max:24B&apps=llama.cpp&sort=trending
- **Лицензия**: MIT