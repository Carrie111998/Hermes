---
title: Slime — пост-тренинг RL для LLM с Megatron и SGLang
sidebar_label: Slime
description: Пост-тренинг RL для LLM с Megatron и SGLang
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Слизь

Пост-тренинг RL для LLM с Megatron и SGLang.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/mlops/slime` |
| Путь | `optional-skills/mlops/slime` |
| Версия | `1.0.0` |
| Автор | Исследование оркестра |
| Лицензия | Массачусетский технологический институт |
| Зависимости | `sglang-router>=0.2.3`, `ray`, `torch>=2.0.0`, `transformers>=4.40.0` |
| Платформы | Linux, MacOS |
| Теги | `Reinforcement Learning`, `Megatron-LM`, `SGLang`, `GRPO`, `Post-Training`, `GLM` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# slime: Рамочная программа постобучения LLM для масштабирования RL

slime — это система постобучения LLM от команды THUDM Цинхуа, лежащая в основе GLM-4.5, GLM-4.6 и GLM-4.7. Он соединяет Megatron-LM для обучения с SGLang для высокопроизводительной генерации развертываний.

## Когда использовать слизь

**Выбирайте слайм, когда вам нужно:**
- Встроенное обучение Megatron-LM с выводом SGLang
- Пользовательские рабочие процессы создания данных с гибкими буферами данных.
- Обучение моделей GLM, Qwen3, DeepSeek V3 или Llama 3.
- Фреймворк исследовательского уровня с производственной поддержкой (Z.ai)

**Рассмотрите альтернативные варианты, если:**
– Вам нужны функции стабильности корпоративного уровня → используйте **мили**
- Вам нужна гибкая замена серверной части → используйте **verl**
- Вам нужны собственные абстракции PyTorch → используйте **torchforge**.

## Ключевые особенности

- **Обучение**: Мегатрон-ЛМ с полной поддержкой параллелизма (ТП, ПП, ДП, СП)
- **Внедрение**: высокопроизводительная генерация на основе SGLang с помощью маршрутизатора.
- **Буфер данных**: гибкое управление подсказками и хранение образцов.
- **Модели**: GLM-4.x, Qwen3, DeepSeek V3/R1, Llama 3.

## Обзор архитектуры

<!-- ascii-guard-ignore -->
```
┌─────────────────────────────────────────────────────────┐
│                    Data Buffer                          │
│ - Prompt initialization and management                  │
│ - Custom data generation and filtering                  │
│ - Rollout sample storage                                │
└─────────────┬───────────────────────────┬───────────────┘
              │                           │
┌─────────────▼───────────┐ ┌─────────────▼───────────────┐
│ Training (Megatron-LM)  │ │ Rollout (SGLang + Router)   │
│ - Actor model training  │ │ - Response generation       │
│ - Critic (optional)     │ │ - Reward/verifier output    │
│ - Weight sync to rollout│ │ - Multi-turn support        │
└─────────────────────────┘ └─────────────────────────────┘
```
<!-- ascii-guard-ignore-end -->

## Установка

```bash
# Recommended: Docker
docker pull slimerl/slime:latest
docker run --rm --gpus all --ipc=host --shm-size=16g \
  -it slimerl/slime:latest /bin/bash

# Inside container
cd /root/slime && pip install -e . --no-deps
```

### Из источника

```bash
git clone https://github.com/THUDM/slime.git
cd slime
pip install -r requirements.txt
pip install -e .
```

## Быстрый старт: обучение GRPO

```bash
# Source model configuration
source scripts/models/qwen3-4B.sh

# Launch training
python train.py \
    --actor-num-nodes 1 \
    --actor-num-gpus-per-node 4 \
    --rollout-num-gpus 4 \
    --advantage-estimator grpo \
    --use-kl-loss --kl-loss-coef 0.001 \
    --rollout-batch-size 32 \
    --n-samples-per-prompt 8 \
    --global-batch-size 256 \
    --num-rollout 3000 \
    --prompt-data /path/to/data.jsonl \
    ${MODEL_ARGS[@]} ${CKPT_ARGS[@]}
```

---

## Рабочий процесс 1: стандартное обучение GRPO

Используйте этот рабочий процесс для обучения моделей рассуждения с преимуществами относительно группы.

### Контрольный список предварительных требований
- [ ] Установлена среда Docker или Megatron-LM + SGLang.
- [ ] Модель контрольной точки (формат HuggingFace или Megatron)
- [ ] Данные обучения в формате JSONL

### Шаг 1. Подготовьте данные

```python
# data.jsonl format
{"prompt": "What is 2 + 2?", "label": "4"}
{"prompt": "Solve: 3x = 12", "label": "x = 4"}
```

Или в формате чата:
```python
{
    "prompt": [
        {"role": "system", "content": "You are a math tutor."},
        {"role": "user", "content": "What is 15 + 27?"}
    ],
    "label": "42"
}
```

### Шаг 2. Настройка модели

Выберите предварительно настроенный сценарий модели:

```bash
# List available models
ls scripts/models/
# glm4-9B.sh, qwen3-4B.sh, qwen3-30B-A3B.sh, deepseek-v3.sh, llama3-8B.sh, ...

# Source your model
source scripts/models/qwen3-4B.sh
```

### Шаг 3. Запуск обучения

```bash
python train.py \
    --actor-num-nodes 1 \
    --actor-num-gpus-per-node 8 \
    --rollout-num-gpus 8 \
    --advantage-estimator grpo \
    --use-kl-loss \
    --kl-loss-coef 0.001 \
    --prompt-data /path/to/train.jsonl \
    --input-key prompt \
    --label-key label \
    --apply-chat-template \
    --rollout-batch-size 32 \
    --n-samples-per-prompt 8 \
    --global-batch-size 256 \
    --num-rollout 3000 \
    --save-interval 100 \
    --eval-interval 50 \
    ${MODEL_ARGS[@]}
```

### Шаг 4: Мониторинг обучения
- [ ] Проверьте TensorBoard: `tensorboard --logdir outputs/`
- [ ] Убедитесь, что кривые вознаграждения растут.
- [] Мониторинг использования графического процессора между узлами

---

## Рабочий процесс 2: асинхронное обучение

Используйте асинхронный режим для повышения пропускной способности за счет перекрытия развертывания и обучения.

### Когда использовать асинхронный режим
- Большие модели с длительным временем генерации
- Высокое время простоя графического процессора в синхронном режиме
- Достаточно памяти для буферизации

### Запуск асинхронного обучения

```bash
python train_async.py \
    --actor-num-nodes 1 \
    --actor-num-gpus-per-node 8 \
    --rollout-num-gpus 8 \
    --advantage-estimator grpo \
    --async-buffer-size 4 \
    --prompt-data /path/to/train.jsonl \
    ${MODEL_ARGS[@]}
```

### Параметры, специфичные для асинхронных вычислений

```bash
--async-buffer-size 4        # Number of rollouts to buffer
--update-weights-interval 2  # Sync weights every N rollouts
```

---

## Рабочий процесс 3: Многоходовое обучение агентов

Используйте этот рабочий процесс для обучения агентов использованию инструментов или многоэтапному рассуждению.

### Предварительные условия
- [ ] Пользовательская функция генерации для многооборотной логики
- [ ] Интерфейс инструмента/среды

### Шаг 1. Определите пользовательскую функцию генерации

```python
# custom_generate.py
async def custom_generate(args, samples, evaluation=False):
    """Multi-turn generation with tool calling."""
    for sample in samples:
        conversation = sample.prompt

        for turn in range(args.max_turns):
            # Generate response
            response = await generate_single(conversation)

            # Check for tool call
            tool_call = extract_tool_call(response)
            if tool_call:
                tool_result = execute_tool(tool_call)
                conversation.append({"role": "assistant", "content": response})
                conversation.append({"role": "tool", "content": tool_result})
            else:
                break

        sample.response = response
        sample.reward = compute_reward(sample)

    return samples
```

### Шаг 2. Запуск с пользовательской функцией

```bash
python train.py \
    --custom-generate-function-path custom_generate.py \
    --max-turns 5 \
    --prompt-data /path/to/agent_data.jsonl \
    ${MODEL_ARGS[@]}
```

См. `examples/search-r1/` для полного примера многооборотного поиска.

---

## Справочник по конфигурации

### Три категории аргументов

Slime использует три типа аргументов:

**1. Аргументы Мегатрона** (передаются напрямую):
```bash
--tensor-model-parallel-size 2
--pipeline-model-parallel-size 1
--num-layers 32
--hidden-size 4096
```

**2. Аргументы SGLang** (с префиксом `--sglang-`):
```bash
--sglang-mem-fraction-static 0.8
--sglang-context-length 8192
--sglang-log-level INFO
```

**3. слизь Аргументы**:
```bash
# Resource allocation
--actor-num-nodes 1
--actor-num-gpus-per-node 8
--rollout-num-gpus 8
--colocate  # Share GPUs between training/inference

# Data
--prompt-data /path/to/data.jsonl
--input-key prompt
--label-key label

# Training loop
--num-rollout 3000
--rollout-batch-size 32
--n-samples-per-prompt 8
--global-batch-size 256

# Algorithm
--advantage-estimator grpo  # or: gspo, ppo, reinforce_plus_plus
--use-kl-loss
--kl-loss-coef 0.001
```

### Ключевые ограничения

```
rollout_batch_size × n_samples_per_prompt = global_batch_size × num_steps_per_rollout
```

Пример: 32 × 8 = 256 × 1.

---

## Система буферизации данных

Буфер данных slime обеспечивает гибкое управление данными:

### Базовый источник данных

```python
class RolloutDataSource:
    def get_samples(self, num_samples):
        """Fetch prompts from dataset."""
        return self.dataset.sample(num_samples)

    def add_samples(self, samples):
        """Called after generation (no-op by default)."""
        pass
```

### Буферизованный источник данных (вне политики)

```python
class RolloutDataSourceWithBuffer(RolloutDataSource):
    def __init__(self):
        self.buffer = []

    def add_samples(self, samples):
        """Store generated samples for reuse."""
        self.buffer.extend(samples)

    def buffer_filter(self, args, buffer, num_samples):
        """Custom selection logic (prioritized, stratified, etc.)."""
        return select_best(buffer, num_samples)
```

---

## Распространенные проблемы и решения

### Проблема: сбой движка SGLang

**Симптомы**: механизм вывода умирает в середине обучения.

**Решения**:
```bash
# Enable fault tolerance
--use-fault-tolerance

# Increase memory allocation
--sglang-mem-fraction-static 0.85

# Reduce batch size
--rollout-batch-size 16
```

### Проблема: тайм-аут синхронизации веса

**Признаки**: Обучение зависает после развертывания.

**Решения**:
```bash
# Increase sync interval
--update-weights-interval 5

# Use colocated mode (no network transfer)
--colocate
```

### Проблема: OOM во время тренировки

**Симптомы**: CUDA OOM при обратном проходе

**Решения**:
```bash
# Enable gradient checkpointing
--recompute-activations

# Reduce micro-batch size
--micro-batch-size 1

# Enable sequence parallelism
--sequence-parallel
```

### Проблема: медленная загрузка данных

**Симптомы**: графический процессор простаивает во время выборки данных.

**Решения**:
```bash
# Increase data workers
--num-data-workers 4

# Use streaming dataset
--streaming-data
```

---

## Поддерживаемые модели

| Модельная семья | Конфигурации |
|--------------|----------------|
| ГЛМ | ГЛМ-4.5, ГЛМ-4.6, ГЛМ-4.7, ГЛМ-З1-9Б |
| Квен | Qwen3 (4B, 8B, 30B-A3B), Qwen3-MoE, Qwen2.5 |
| ДипСик | V3, V3.1, R1 |
| Лама | Лама 3 (8Б, 70Б) |
| Другие | Кими К2, Лунный свет-16Б |

Каждая модель имеет предварительно настроенные сценарии в `scripts/models/`.

---

## Расширенные темы

### Режим совместного размещения

Разделяйте графические процессоры между обучением и логическим выводом, чтобы уменьшить объем памяти:

```bash
python train.py \
    --colocate \
    --actor-num-gpus-per-node 8 \
    --sglang-mem-fraction-static 0.4 \
    ${MODEL_ARGS[@]}
```

### Пользовательская модель вознаграждения

```python
# custom_rm.py
class CustomRewardModel:
    def __init__(self, model_path):
        self.model = load_model(model_path)

    def compute_reward(self, prompts, responses):
        inputs = self.tokenize(prompts, responses)
        scores = self.model(inputs)
        return scores.tolist()
```

```bash
--custom-rm-path custom_rm.py
```

### Оценка многозадачности

```bash
--eval-prompt-data aime /path/to/aime.jsonl \
--eval-prompt-data gsm8k /path/to/gsm8k.jsonl \
--n-samples-per-eval-prompt 16
```

---

## Ресурсы

- **Документация**: https://thudm.github.io/slime/
- **GitHub**: https://github.com/THUDM/slime
- **Блог**: https://lmsys.org/blog/2025-07-09-slime/
- **Примеры**: см. каталог `examples/`, где представлено более 14 рабочих примеров.