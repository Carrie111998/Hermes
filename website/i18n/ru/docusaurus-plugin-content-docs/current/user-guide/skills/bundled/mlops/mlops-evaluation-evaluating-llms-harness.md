---
title: 'Оценка Llms Harness — lm-eval-harness: эталонные LLM (MMLU, GSM8K и т. д.)'
sidebar_label: Evaluating Llms Harness
description: 'lm-eval-harness: эталонные LLM (MMLU, GSM8K и т. д.)'
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Оценка ремня Llms

lm-eval-harness: эталонные LLM (MMLU, GSM8K и т. д.).

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/mlops/evaluation/evaluating-llms-harness` |
| Версия | `1.0.1` |
| Автор | Исследование оркестра |
| Лицензия | Массачусетский технологический институт |
| Зависимости | `lm-eval`, `transformers`, `vllm` |
| Платформы | Linux, MacOS |
| Теги | `Evaluation`, `LM Evaluation Harness`, `Benchmarking`, `MMLU`, `HumanEval`, `GSM8K`, `EleutherAI`, `Model Quality`, `Academic Benchmarks`, `Industry Standard` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# lm-evaluation-harness - Бенчмаркинг LLM

## Что внутри

Оценивает LLM по более чем 60 академическим критериям (MMLU, HumanEval, GSM8K, TruthfulQA, HellaSwag). Используйте при сравнительном анализе качества модели, сравнении моделей, составлении отчетов об академических результатах или отслеживании прогресса обучения. Отраслевой стандарт, используемый EleutherAI, HuggingFace и крупными лабораториями. Поддерживает HuggingFace, vLLM, API.

## Быстрый старт

lm-evaluation-harness оценивает LLM по более чем 60 академическим критериям, используя стандартизированные подсказки и показатели.

**Установка**:
```bash
pip install lm-eval
```

**Оцените любую модель HuggingFace**:
```bash
lm_eval --model hf \
  --model_args pretrained=meta-llama/Llama-2-7b-hf \
  --tasks mmlu,gsm8k,hellaswag \
  --device cuda:0 \
  --batch_size 8
```

**Просмотр доступных задач**:
```bash
lm-eval ls tasks
```

## Общие рабочие процессы

### Рабочий процесс 1: стандартная контрольная оценка

Оцените модель с помощью основных тестов (MMLU, GSM8K, HumanEval).

Скопируйте этот контрольный список:

```
Benchmark Evaluation:
- [ ] Step 1: Choose benchmark suite
- [ ] Step 2: Configure model
- [ ] Step 3: Run evaluation
- [ ] Step 4: Analyze results
```

**Шаг 1. Выберите набор тестов**

**Основные критерии рассуждения**:
- **MMLU** (Массовое многозадачное понимание языка) - 57 предметов, множественный выбор
- **GSM8K** - Задачи по математике в начальной школе
- **HellaSwag** - Рассуждения, основанные на здравом смысле
- **TruthfulQA** - Правдивость и достоверность
- **ARC** (Задание на рассуждение AI2) – Научные вопросы

**Бенчмарки кода**:
- **HumanEval** — генерация кода Python (164 задачи)
- **MBPP** (в основном базовые проблемы Python) - Кодирование на Python

**Стандартный пакет** (рекомендуется для моделей):
```bash
--tasks mmlu,gsm8k,hellaswag,truthfulqa,arc_challenge
```

**Шаг 2. Настройте модель**

**Модель HuggingFace**:
```bash
lm_eval --model hf \
  --model_args pretrained=meta-llama/Llama-2-7b-hf,dtype=bfloat16 \
  --tasks mmlu \
  --device cuda:0 \
  --batch_size auto  # Auto-detect optimal batch size
```

**Квантованная модель (4-битная/8-битная)**:
```bash
lm_eval --model hf \
  --model_args pretrained=meta-llama/Llama-2-7b-hf,load_in_4bit=True \
  --tasks mmlu \
  --device cuda:0
```

**Пользовательская контрольная точка**:
```bash
lm_eval --model hf \
  --model_args pretrained=/path/to/my-model,tokenizer=/path/to/tokenizer \
  --tasks mmlu \
  --device cuda:0
```

**Шаг 3. Запустите оценку**

```bash
# Full MMLU evaluation (57 subjects)
lm_eval --model hf \
  --model_args pretrained=meta-llama/Llama-2-7b-hf \
  --tasks mmlu \
  --num_fewshot 5 \  # 5-shot evaluation (standard)
  --batch_size 8 \
  --output_path results/ \
  --log_samples  # Save individual predictions

# Multiple benchmarks at once
lm_eval --model hf \
  --model_args pretrained=meta-llama/Llama-2-7b-hf \
  --tasks mmlu,gsm8k,hellaswag,truthfulqa,arc_challenge \
  --num_fewshot 5 \
  --batch_size 8 \
  --output_path results/llama2-7b-eval.json
```

**Шаг 4. Анализ результатов**

Результаты сохранены в `results/llama2-7b-eval.json`:

```json
{
  "results": {
    "mmlu": {
      "acc": 0.459,
      "acc_stderr": 0.004
    },
    "gsm8k": {
      "exact_match": 0.142,
      "exact_match_stderr": 0.006
    },
    "hellaswag": {
      "acc_norm": 0.765,
      "acc_norm_stderr": 0.004
    }
  },
  "config": {
    "model": "hf",
    "model_args": "pretrained=meta-llama/Llama-2-7b-hf",
    "num_fewshot": 5
  }
}
```

### Рабочий процесс 2: отслеживание прогресса обучения

Оценивайте контрольные точки во время обучения.

```
Training Progress Tracking:
- [ ] Step 1: Set up periodic evaluation
- [ ] Step 2: Choose quick benchmarks
- [ ] Step 3: Automate evaluation
- [ ] Step 4: Plot learning curves
```

**Шаг 1. Настройте периодическую оценку**

Оцените каждые N шагов обучения:

```bash
#!/bin/bash
# eval_checkpoint.sh

CHECKPOINT_DIR=$1
STEP=$2

lm_eval --model hf \
  --model_args pretrained=$CHECKPOINT_DIR/checkpoint-$STEP \
  --tasks gsm8k,hellaswag \
  --num_fewshot 0 \  # 0-shot for speed
  --batch_size 16 \
  --output_path results/step-$STEP.json
```

**Шаг 2. Выберите быстрые тесты**

Быстрые тесты для частой оценки:
- **HellaSwag**: ~10 минут на 1 графическом процессоре.
- **GSM8K**: ~5 минут
- **ПИКА**: ~2 минуты.

Избегайте частой оценки (слишком медленно):
- **MMLU**: ~2 часа (57 субъектов)
- **HumanEval**: требует выполнения кода.

**Шаг 3. Автоматизируйте оценку**

Интеграция со сценарием обучения:

```python
# In training loop
if step % eval_interval == 0:
    model.save_pretrained(f"checkpoints/step-{step}")

    # Run evaluation
    os.system(f"./eval_checkpoint.sh checkpoints step-{step}")
```

Или используйте обратные вызовы PyTorch Lightning:

```python
from pytorch_lightning import Callback

class EvalHarnessCallback(Callback):
    def on_validation_epoch_end(self, trainer, pl_module):
        step = trainer.global_step
        checkpoint_path = f"checkpoints/step-{step}"

        # Save checkpoint
        trainer.save_checkpoint(checkpoint_path)

        # Run lm-eval
        os.system(f"lm_eval --model hf --model_args pretrained={checkpoint_path} ...")
```

**Шаг 4. Постройте кривые обучения**

```python
import json
import matplotlib.pyplot as plt

# Load all results
steps = []
mmlu_scores = []

for file in sorted(glob.glob("results/step-*.json")):
    with open(file) as f:
        data = json.load(f)
        step = int(file.split("-")[1].split(".")[0])
        steps.append(step)
        mmlu_scores.append(data["results"]["mmlu"]["acc"])

# Plot
plt.plot(steps, mmlu_scores)
plt.xlabel("Training Step")
plt.ylabel("MMLU Accuracy")
plt.title("Training Progress")
plt.savefig("training_curve.png")
```

### Рабочий процесс 3: сравнение нескольких моделей

Пакет тестов для сравнения моделей.

```
Model Comparison:
- [ ] Step 1: Define model list
- [ ] Step 2: Run evaluations
- [ ] Step 3: Generate comparison table
```

**Шаг 1. Определите список моделей**

```bash
# models.txt
meta-llama/Llama-2-7b-hf
meta-llama/Llama-2-13b-hf
mistralai/Mistral-7B-v0.1
microsoft/phi-2
```

**Шаг 2. Проведите оценку**

```bash
#!/bin/bash
# eval_all_models.sh

TASKS="mmlu,gsm8k,hellaswag,truthfulqa"

while read model; do
    echo "Evaluating $model"

    # Extract model name for output file
    model_name=$(echo $model | sed 's/\//-/g')

    lm_eval --model hf \
      --model_args pretrained=$model,dtype=bfloat16 \
      --tasks $TASKS \
      --num_fewshot 5 \
      --batch_size auto \
      --output_path results/$model_name.json

done < models.txt
```

**Шаг 3. Создайте сравнительную таблицу**

```python
import json
import pandas as pd

models = [
    "meta-llama-Llama-2-7b-hf",
    "meta-llama-Llama-2-13b-hf",
    "mistralai-Mistral-7B-v0.1",
    "microsoft-phi-2"
]

tasks = ["mmlu", "gsm8k", "hellaswag", "truthfulqa"]

results = []
for model in models:
    with open(f"results/{model}.json") as f:
        data = json.load(f)
        row = {"Model": model.replace("-", "/")}
        for task in tasks:
            # Get primary metric for each task
            metrics = data["results"][task]
            if "acc" in metrics:
                row[task.upper()] = f"{metrics['acc']:.3f}"
            elif "exact_match" in metrics:
                row[task.upper()] = f"{metrics['exact_match']:.3f}"
        results.append(row)

df = pd.DataFrame(results)
print(df.to_markdown(index=False))
```

Выход:
```
| Model                  | MMLU  | GSM8K | HELLASWAG | TRUTHFULQA |
|------------------------|-------|-------|-----------|------------|
| meta-llama/Llama-2-7b  | 0.459 | 0.142 | 0.765     | 0.391      |
| meta-llama/Llama-2-13b | 0.549 | 0.287 | 0.801     | 0.430      |
| mistralai/Mistral-7B   | 0.626 | 0.395 | 0.812     | 0.428      |
| microsoft/phi-2        | 0.560 | 0.613 | 0.682     | 0.447      |
```

### Рабочий процесс 4: оценка с помощью vLLM (более быстрый вывод)

Используйте серверную часть vLLM для ускорения оценки в 5–10 раз.

```
vLLM Evaluation:
- [ ] Step 1: Install vLLM
- [ ] Step 2: Configure vLLM backend
- [ ] Step 3: Run evaluation
```

**Шаг 1. Установите vLLM**

```bash
pip install vllm
```

**Шаг 2. Настройте серверную часть vLLM**

```bash
lm_eval --model vllm \
  --model_args pretrained=meta-llama/Llama-2-7b-hf,tensor_parallel_size=1,dtype=auto,gpu_memory_utilization=0.8 \
  --tasks mmlu \
  --batch_size auto
```

**Шаг 3. Запустите оценку**

vLLM в 5-10 раз быстрее стандартного HuggingFace:

```bash
# Standard HF: ~2 hours for MMLU on 7B model
lm_eval --model hf \
  --model_args pretrained=meta-llama/Llama-2-7b-hf \
  --tasks mmlu \
  --batch_size 8

# vLLM: ~15-20 minutes for MMLU on 7B model
lm_eval --model vllm \
  --model_args pretrained=meta-llama/Llama-2-7b-hf,tensor_parallel_size=2 \
  --tasks mmlu \
  --batch_size auto
```

## Когда использовать альтернативы

**Используйте lm-evaluation-harness, когда:**
- Модели сравнительного анализа научных работ.
- Сравнение качества модели в стандартных задачах.
- Отслеживание прогресса обучения
- Отчетность по стандартизированным показателям (все используют одни и те же подсказки)
- Нужна воспроизводимая оценка

**Вместо этого используйте альтернативы:**
- **HELM** (Стэнфорд): более широкая оценка (справедливость, эффективность, калибровка)
- **AlpacaEval**: оценка после выполнения инструкций судьями LLM.
- **MT-Bench**: многоходовая диалоговая оценка.
- **Пользовательские сценарии**: оценка для конкретного домена.

## Распространенные проблемы

**Проблема: оценка выполняется слишком медленно**

Используйте серверную часть vLLM:
```bash
lm_eval --model vllm \
  --model_args pretrained=model-name,tensor_parallel_size=2
```

Или сократите несколько примеров:
```bash
--num_fewshot 0  # Instead of 5
```

Или оцените подмножество MMLU:
```bash
--tasks mmlu_stem  # Only STEM subjects
```

**Проблема: недостаточно памяти**

Уменьшить размер партии:
```bash
--batch_size 1  # Or --batch_size auto
```

Используйте квантование:
```bash
--model_args pretrained=model-name,load_in_8bit=True
```

Включите разгрузку процессора:
```bash
--model_args pretrained=model-name,device_map=auto,offload_folder=offload
```

**Проблема: результаты отличаются от заявленных**

Проверьте количество выстрелов:
```bash
--num_fewshot 5  # Most papers use 5-shot
```

Проверьте точное название задачи:
```bash
--tasks mmlu  # Not mmlu_direct or mmlu_fewshot
```

Проверьте соответствие модели и токенизатора:
```bash
--model_args pretrained=model-name,tokenizer=same-model-name
```

**Проблема: HumanEval не выполняет код**

Задачи выполнения кода (HumanEval, MBPP и т. д.) закрываются явным
флаг подтверждения — для их запуска необходимо передать `--confirm_run_unsafe_code`:

```bash
lm_eval --model hf \
  --model_args pretrained=model-name \
  --tasks humaneval \
  --confirm_run_unsafe_code  # Required to run tasks that execute generated code
```

Без этого флага lm-eval отказывается запускать задачу, а не просто пропускает ее.
выполнение кода.

## Расширенные темы

**Описания тестов**: см. [references/benchmark-guide.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/mlops/evaluation/evaluating-llms-harness/references/benchmark-guide.md) для подробного описания всех более чем 60 задач, их измерения и интерпретации.

**Пользовательские задачи**: см. [references/custom-tasks.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/mlops/evaluation/evaluating-llms-harness/references/custom-tasks.md) для создания задач оценки для конкретной предметной области.

**Оценка API**: см. [references/api-evaluation.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/mlops/evaluation/evaluating-llms-harness/references/api-evaluation.md) для оценки OpenAI, Anthropic и других моделей API.

**Стратегии с несколькими графическими процессорами**: см. [references/distributed-eval.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/mlops/evaluation/evaluating-llms-harness/references/distributed-eval.md) для параллельной оценки данных и тензорной параллельности.

## Требования к оборудованию

- **ГП**: NVIDIA (CUDA 11.8+), работает на ЦП (очень медленно)
- **ВОЗУ**:
  - Модель 7B: 16 ГБ (bf16) или 8 ГБ (8-разрядная версия)
  - Модель 13B: 28 ГБ (bf16) или 14 ГБ (8-бит)
  - Модель 70B: требуется несколько графических процессоров или квантование.
- **Время** (модель 7B, одиночный A100):
  - ХеллаСваг: 10 минут
  - GSM8K: 5 минут
  - MMLU (полный): 2 часа
  - HumanEval: 20 минут

## Ресурсы

- GitHub: https://github.com/EleutherAI/lm-evaluation-harness
- Документы: https://github.com/EleutherAI/lm-evaluation-harness/tree/main/docs.
- Библиотека задач: более 60 задач, включая MMLU, GSM8K, HumanEval, TruthfulQA, HellaSwag, ARC, WinoGrande и т. д.
- Таблица лидеров: https://huggingface.co/spaces/HuggingFaceH4/open_llm_leaderboard (используется эта подвеска)