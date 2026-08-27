---
title: 'Обслуживание Llms Vllm — vLLM: высокопроизводительное обслуживание LLM, API
  OpenAI, квантование'
sidebar_label: Serving Llms Vllm
description: 'vLLM: высокопроизводительное обслуживание LLM, OpenAI API, квантование'
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Обслуживание Llms Vllm

vLLM: высокопроизводительное обслуживание LLM, OpenAI API, квантование.

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/mlops/inference/serving-llms-vllm` |
| Версия | `1.0.1` |
| Автор | Исследование оркестра |
| Лицензия | Массачусетский технологический институт |
| Зависимости | `vllm`, `torch`, `transformers` |
| Платформы | Linux, MacOS |
| Теги | `vLLM`, `Inference Serving`, `PagedAttention`, `Continuous Batching`, `High Throughput`, `Production`, `OpenAI API`, `Quantization`, `Tensor Parallelism` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# vLLM — Высокопроизводительное обслуживание LLM

## Когда использовать

Используйте при развертывании производственных API-интерфейсов LLM, оптимизации задержки/пропускной способности вывода или обслуживания моделей с ограниченной памятью графического процессора. Поддерживает OpenAI-совместимые конечные точки, квантование (GPTQ/AWQ/FP8) и тензорный параллелизм.

## Быстрый старт

vLLM обеспечивает в 24 раза более высокую пропускную способность, чем стандартные преобразователи, благодаря PagedAttention (блочный KV-кэш) и непрерывной пакетной обработке (смешивание запросов предварительного заполнения и декодирования).

**Установка**:
```bash
pip install vllm
```

**Основной офлайн-вывод**:
```python
from vllm import LLM, SamplingParams

llm = LLM(model="meta-llama/Meta-Llama-3-8B-Instruct")
sampling = SamplingParams(temperature=0.7, max_tokens=256)

outputs = llm.generate(["Explain quantum computing"], sampling)
print(outputs[0].outputs[0].text)
```

**Сервер, совместимый с OpenAI**:
```bash
vllm serve meta-llama/Meta-Llama-3-8B-Instruct

# Query with OpenAI SDK
python -c "
from openai import OpenAI
client = OpenAI(base_url='http://localhost:8000/v1', api_key='EMPTY')
print(client.chat.completions.create(
    model='meta-llama/Meta-Llama-3-8B-Instruct',
    messages=[{'role': 'user', 'content': 'Hello!'}]
).choices[0].message.content)
"
```

## Общие рабочие процессы

### Рабочий процесс 1. Развертывание производственного API

Скопируйте этот контрольный список и отслеживайте прогресс:

```
Deployment Progress:
- [ ] Step 1: Configure server settings
- [ ] Step 2: Test with limited traffic
- [ ] Step 3: Enable monitoring
- [ ] Step 4: Deploy to production
- [ ] Step 5: Verify performance metrics
```

**Шаг 1. Настройте параметры сервера**

Выберите конфигурацию в зависимости от размера вашей модели:

```bash
# For 7B-13B models on single GPU
vllm serve meta-llama/Meta-Llama-3-8B-Instruct \
  --gpu-memory-utilization 0.9 \
  --max-model-len 8192 \
  --port 8000

# For 30B-70B models with tensor parallelism
vllm serve meta-llama/Meta-Llama-3-70B-Instruct \
  --tensor-parallel-size 4 \
  --gpu-memory-utilization 0.9 \
  --quantization awq \
  --port 8000

# For production with caching (Prometheus metrics are exposed
# automatically at /metrics on the API port)
vllm serve meta-llama/Meta-Llama-3-8B-Instruct \
  --gpu-memory-utilization 0.9 \
  --enable-prefix-caching \
  --port 8000 \
  --host 0.0.0.0
```

**Шаг 2. Тестируйте с ограниченным трафиком**

Запустите нагрузочное тестирование перед производством:

```bash
# Install load testing tool
pip install locust

# Create test_load.py with sample requests
# Run: locust -f test_load.py --host http://localhost:8000
```

Проверьте TTFT (время до первого токена) &lt; 500 мс и пропускная способность > 100 запросов/сек.

**Шаг 3. Включите мониторинг**

vLLM предоставляет метрики Prometheus по адресу `/metrics` порта API (по умолчанию 8000):

```bash
curl http://localhost:8000/metrics | grep vllm
```

Ключевые показатели для мониторинга:
- `vllm:time_to_first_token_seconds` - Задержка
- `vllm:num_requests_running` - Активные запросы
- `vllm:gpu_cache_usage_perc` - Использование кэша KV

**Шаг 4. Развертывание в рабочей среде**

Используйте Docker для согласованного развертывания:

```bash
# Run vLLM in Docker
docker run --gpus all -p 8000:8000 \
  vllm/vllm-openai:latest \
  --model meta-llama/Meta-Llama-3-8B-Instruct \
  --gpu-memory-utilization 0.9 \
  --enable-prefix-caching
```

**Шаг 5. Проверьте показатели производительности**

Убедитесь, что развертывание соответствует целям:
- ТТФТ &lt; 500 мс (для коротких подсказок)
- Пропускная способность > целевого запроса/сек.
- Загрузка графического процессора > 80 %
- Нет ошибок OOM в журналах

### Рабочий процесс 2: пакетный вывод в автономном режиме

Для обработки больших наборов данных без нагрузки на сервер.

Скопируйте этот контрольный список:

```
Batch Processing:
- [ ] Step 1: Prepare input data
- [ ] Step 2: Configure LLM engine
- [ ] Step 3: Run batch inference
- [ ] Step 4: Process results
```

**Шаг 1. Подготовьте входные данные**

```python
# Load prompts from file
prompts = []
with open("prompts.txt") as f:
    prompts = [line.strip() for line in f]

print(f"Loaded {len(prompts)} prompts")
```

**Шаг 2. Настройте модуль LLM**

```python
from vllm import LLM, SamplingParams

llm = LLM(
    model="meta-llama/Meta-Llama-3-8B-Instruct",
    tensor_parallel_size=2,  # Use 2 GPUs
    gpu_memory_utilization=0.9,
    max_model_len=4096
)

sampling = SamplingParams(
    temperature=0.7,
    top_p=0.95,
    max_tokens=512,
    stop=["</s>", "\n\n"]
)
```

**Шаг 3. Запустите пакетный вывод**

vLLM автоматически группирует запросы для повышения эффективности:

```python
# Process all prompts in one call
outputs = llm.generate(prompts, sampling)

# vLLM handles batching internally
# No need to manually chunk prompts
```

**Шаг 4. Обработка результатов**

```python
# Extract generated text
results = []
for output in outputs:
    prompt = output.prompt
    generated = output.outputs[0].text
    results.append({
        "prompt": prompt,
        "generated": generated,
        "tokens": len(output.outputs[0].token_ids)
    })

# Save to file
import json
with open("results.jsonl", "w") as f:
    for result in results:
        f.write(json.dumps(result) + "\n")

print(f"Processed {len(results)} prompts")
```

### Рабочий процесс 3. Обслуживание квантовой модели

Поместите большие модели в ограниченную память графического процессора.

```
Quantization Setup:
- [ ] Step 1: Choose quantization method
- [ ] Step 2: Find or create quantized model
- [ ] Step 3: Launch with quantization flag
- [ ] Step 4: Verify accuracy
```

**Шаг 1. Выберите метод квантования**

- **AWQ**: лучше всего подходит для моделей 70B, минимальная потеря точности.
- **GPTQ**: широкая поддержка моделей, хорошее сжатие.
- **FP8**: самый быстрый на графических процессорах H100.

**Шаг 2. Найдите или создайте квантованную модель**

Используйте предварительно квантованные модели из HuggingFace:

```bash
# Search for AWQ models
# Example: TheBloke/Llama-2-70B-AWQ
```

**Шаг 3. Запуск с флагом квантования**

```bash
# Using pre-quantized model
vllm serve TheBloke/Llama-2-70B-AWQ \
  --quantization awq \
  --tensor-parallel-size 1 \
  --gpu-memory-utilization 0.95

# Results: 70B model in ~40GB VRAM
```

**Шаг 4. Проверьте точность**

Результаты тестирования соответствуют ожидаемому качеству:

```python
# Compare quantized vs non-quantized responses
# Verify task-specific performance unchanged
```

## Когда использовать альтернативы

**Используйте vLLM, когда:**
- Развертывание производственных API LLM (более 100 запросов в секунду)
- Обслуживание конечных точек, совместимых с OpenAI.
- Ограниченная память графического процессора, но нужны большие модели
- Многопользовательские приложения (чат-боты, помощники)
- Нужна низкая задержка с высокой пропускной способностью

**Вместо этого используйте альтернативы:**
- **llama.cpp**: определение ЦП/границы, однопользовательский режим.
- **Трансформеры HuggingFace**: исследования, прототипирование, разовая генерация.
- **TensorRT-LLM**: только для NVIDIA, требуется абсолютная максимальная производительность.
- **Генерация текста**: уже в экосистеме HuggingFace.

## Распространенные проблемы

**Проблема: не хватает памяти во время загрузки модели**

Уменьшите использование памяти:
```bash
vllm serve MODEL \
  --gpu-memory-utilization 0.7 \
  --max-model-len 4096
```

Или используйте квантование:
```bash
vllm serve MODEL --quantization awq
```

**Проблема: медленный первый токен (TTFT > 1 секунды)**

Включите кэширование префиксов для повторяющихся запросов:
```bash
vllm serve MODEL --enable-prefix-caching
```

Для длинных запросов включите фрагментированное предварительное заполнение:
```bash
vllm serve MODEL --enable-chunked-prefill
```

**Проблема: ошибка «Модель не найдена»**

Используйте `--trust-remote-code` для пользовательских моделей:
```bash
vllm serve MODEL --trust-remote-code
```

**Проблема: низкая пропускная способность (&lt;50 запросов в секунду)**

Увеличение одновременных последовательностей:
```bash
vllm serve MODEL --max-num-seqs 512
```

Проверьте загрузку графического процессора с помощью `nvidia-smi` — она должна быть >80%.

**Проблема: вывод выполняется медленнее, чем ожидалось**

Убедитесь, что тензорный параллелизм использует мощность двух графических процессоров:
```bash
vllm serve MODEL --tensor-parallel-size 4  # Not 3
```

Включите спекулятивное декодирование для более быстрой генерации (передайте конфигурацию как JSON;
`--speculative-model` был удален в пользу `--speculative-config`):
```bash
vllm serve MODEL \
  --speculative-config '{"model": "DRAFT_MODEL", "num_speculative_tokens": 5, "method": "draft_model"}'
```

## Расширенные темы

**Схемы развертывания серверов**: см. [references/server-deployment.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/mlops/inference/serving-llms-vllm/references/server-deployment.md) для конфигураций Docker, Kubernetes и балансировки нагрузки.

**Оптимизация производительности**: см. [references/optimization.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/mlops/inference/serving-llms-vllm/references/optimization.md) для настройки PagedAttention, подробностей непрерывной пакетной обработки и результатов тестов.

**Руководство по квантованию**: см. [references/quantization.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/mlops/inference/serving-llms-vllm/references/quantization.md) для настройки AWQ/GPTQ/FP8, подготовки модели и сравнения точности.

**Устранение неполадок**: см. [references/troubleshooting.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/mlops/inference/serving-llms-vllm/references/troubleshooting.md) для получения подробных сообщений об ошибках, шагов по отладке и диагностики производительности.

## Требования к оборудованию

- **Малые модели (7B–13B)**: 1x A10 (24 ГБ) или A100 (40 ГБ)
- **Средние модели (30B-40B)**: 2x A100 (40 ГБ) с тензорным параллелизмом
- **Большие модели (70B+)**: 4x A100 (40 ГБ) или 2x A100 (80 ГБ), используйте AWQ/GPTQ.

Поддерживаемые платформы: NVIDIA (основная), AMD ROCm, графические процессоры Intel, TPU.

## Ресурсы

- Официальные документы: https://docs.vllm.ai.
- GitHub: https://github.com/vllm-project/vllm
- Документ: «Эффективное управление памятью для обслуживания больших языковых моделей с помощью PagedAttention» (SOSP 2023).
- Сообщество: https://discuss.vllm.ai