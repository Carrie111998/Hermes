---
title: 'Точная настройка Trl — TRL: моделирование вознаграждений SFT, DPO, GRPO, RLOO
  для LLM RLHF'
sidebar_label: Trl Fine Tuning
description: 'TRL: моделирование вознаграждений SFT, DPO, GRPO, RLOO для LLM RLHF.'
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Точная настройка Trl

TRL: моделирование вознаграждений SFT, DPO, GRPO, RLOO для LLM RLHF.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/mlops/trl-fine-tuning` |
| Путь | `optional-skills/mlops/training/trl-fine-tuning` |
| Версия | `1.0.1` |
| Автор | Исследование оркестра |
| Лицензия | Массачусетский технологический институт |
| Зависимости | `trl`, `transformers`, `datasets`, `peft`, `accelerate`, `torch` |
| Платформы | Linux, MacOS, Windows |
| Теги | `Post-Training`, `TRL`, `Reinforcement Learning`, `Fine-Tuning`, `SFT`, `DPO`, `GRPO`, `RLOO`, `RLHF`, `Preference Alignment`, `HuggingFace` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# TRL — Обучение армированию трансформаторов

## Быстрый старт

TRL предоставляет методы постобучения для согласования языковых моделей с предпочтениями человека.

**Установка**:
```bash
pip install trl transformers datasets peft accelerate
```

**Контролируемая точная настройка** (настройка по инструкции):
```python
from trl import SFTTrainer

trainer = SFTTrainer(
    model="Qwen/Qwen2.5-0.5B",
    train_dataset=dataset,  # Prompt-completion pairs
)
trainer.train()
```

**DPO** (в соответствии с предпочтениями):
```python
from trl import DPOTrainer, DPOConfig

config = DPOConfig(output_dir="model-dpo", beta=0.1)
trainer = DPOTrainer(
    model=model,
    args=config,
    train_dataset=preference_dataset,  # chosen/rejected pairs
    processing_class=tokenizer
)
trainer.train()
```

## Общие рабочие процессы

### Рабочий процесс 1: Полный конвейер RLHF (SFT → Модель вознаграждения → RLOO)

Полный конвейер от базовой модели до модели, ориентированной на человека.

> **Примечание (TRL 1.x):** PPO **удалён** из TRL — `PPOTrainer`, `PPOConfig` и
> `python -m trl.scripts.ppo` больше не существует. Используйте онлайн-тренажер RL. TRL все еще поставляется:
> **RLOO** (`RLOOTrainer` / `trl rloo`) — ближайший аналог модели вознаграждения.
> конвейер RLHF, а **GRPO** (`GRPOTrainer` / `trl grpo`, см. рабочий процесс 3)
> Альтернатива с эффективным использованием памяти. На следующем шаге используется RLOO.

Скопируйте этот контрольный список:

```
RLHF Training:
- [ ] Step 1: Supervised fine-tuning (SFT)
- [ ] Step 2: Train reward model
- [ ] Step 3: RLOO reinforcement learning
- [ ] Step 4: Evaluate aligned model
```

**Шаг 1. Контролируемая точная настройка**

Обучить базовую модель на данных, следующих инструкциям:

```python
from transformers import AutoModelForCausalLM, AutoTokenizer
from trl import SFTTrainer, SFTConfig
from datasets import load_dataset

# Load model
model = AutoModelForCausalLM.from_pretrained("Qwen/Qwen2.5-0.5B")
tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-0.5B")

# Load instruction dataset
dataset = load_dataset("trl-lib/Capybara", split="train")

# Configure training
training_args = SFTConfig(
    output_dir="Qwen2.5-0.5B-SFT",
    per_device_train_batch_size=4,
    num_train_epochs=1,
    learning_rate=2e-5,
    logging_steps=10,
    save_strategy="epoch"
)

# Train
trainer = SFTTrainer(
    model=model,
    args=training_args,
    train_dataset=dataset,
    processing_class=tokenizer
)
trainer.train()
trainer.save_model()
```

**Шаг 2. Модель вознаграждения за обучение**

Модель обучения для прогнозирования предпочтений человека:

```python
from transformers import AutoModelForSequenceClassification
from trl import RewardTrainer, RewardConfig

# Load SFT model as base
model = AutoModelForSequenceClassification.from_pretrained(
    "Qwen2.5-0.5B-SFT",
    num_labels=1  # Single reward score
)
tokenizer = AutoTokenizer.from_pretrained("Qwen2.5-0.5B-SFT")

# Load preference data (chosen/rejected pairs)
dataset = load_dataset("trl-lib/ultrafeedback_binarized", split="train")

# Configure training
training_args = RewardConfig(
    output_dir="Qwen2.5-0.5B-Reward",
    per_device_train_batch_size=2,
    num_train_epochs=1,
    learning_rate=1e-5
)

# Train reward model
trainer = RewardTrainer(
    model=model,
    args=training_args,
    processing_class=tokenizer,
    train_dataset=dataset
)
trainer.train()
trainer.save_model()
```

**Шаг 3. Обучение с подкреплением RLOO**

Оптимизируйте политику, используя модель вознаграждения. PPO был удален в TRL 1.x; используйте интерфейс командной строки RLOO
(`trl rloo`) с обученной моделью вознаграждения, переданной через `--reward_model_name_or_path`:

```bash
trl rloo \
    --model_name_or_path Qwen2.5-0.5B-SFT \
    --reward_model_name_or_path Qwen2.5-0.5B-Reward \
    --dataset_name trl-internal-testing/descriptiveness-sentiment-trl-style \
    --output_dir Qwen2.5-0.5B-RLOO \
    --learning_rate 3e-6 \
    --per_device_train_batch_size 64 \
    --num_generations 4
```

Эквивалент Python (`RLOOTrainer`/`RLOOConfig`):
```python
from trl import RLOOTrainer, RLOOConfig
from transformers import AutoModelForSequenceClassification, AutoTokenizer

reward_model = AutoModelForSequenceClassification.from_pretrained(
    "Qwen2.5-0.5B-Reward", num_labels=1
)

config = RLOOConfig(
    output_dir="Qwen2.5-0.5B-RLOO",
    per_device_train_batch_size=64,
    learning_rate=3e-6,
    num_generations=4,
)

trainer = RLOOTrainer(
    model="Qwen2.5-0.5B-SFT",
    reward_funcs=reward_model,   # a reward model (or a callable reward function)
    args=config,
    train_dataset=dataset,       # prompt-only dataset
    processing_class=tokenizer,
)
trainer.train()
```

**Шаг 4. Оценка**

```python
from transformers import pipeline

# Load aligned model
generator = pipeline("text-generation", model="Qwen2.5-0.5B-RLOO")

# Test
prompt = "Explain quantum computing to a 10-year-old"
output = generator(prompt, max_length=200)[0]["generated_text"]
print(output)
```

### Рабочий процесс 2: простое согласование предпочтений с помощью DPO

Приведите модель в соответствие с моделью предпочтений без вознаграждения.

Скопируйте этот контрольный список:

```
DPO Training:
- [ ] Step 1: Prepare preference dataset
- [ ] Step 2: Configure DPO
- [ ] Step 3: Train with DPOTrainer
- [ ] Step 4: Evaluate alignment
```

**Шаг 1. Подготовьте набор данных о предпочтениях**

Формат набора данных:
```json
{
  "prompt": "What is the capital of France?",
  "chosen": "The capital of France is Paris.",
  "rejected": "I don't know."
}
```

Загрузить набор данных:
```python
from datasets import load_dataset

dataset = load_dataset("trl-lib/ultrafeedback_binarized", split="train")
# Or load your own
# dataset = load_dataset("json", data_files="preferences.json")
```

**Шаг 2. Настройте DPO**

```python
from trl import DPOConfig

config = DPOConfig(
    output_dir="Qwen2.5-0.5B-DPO",
    per_device_train_batch_size=4,
    num_train_epochs=1,
    learning_rate=5e-7,
    beta=0.1,  # KL penalty strength
    max_prompt_length=512,
    max_length=1024,
    logging_steps=10
)
```

**Шаг 3. Тренируйтесь с DPOtrainer**

```python
from transformers import AutoModelForCausalLM, AutoTokenizer
from trl import DPOTrainer

model = AutoModelForCausalLM.from_pretrained("Qwen/Qwen2.5-0.5B-Instruct")
tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-0.5B-Instruct")

trainer = DPOTrainer(
    model=model,
    args=config,
    train_dataset=dataset,
    processing_class=tokenizer
)

trainer.train()
trainer.save_model()
```

**Альтернатива CLI**:
```bash
trl dpo \
    --model_name_or_path Qwen/Qwen2.5-0.5B-Instruct \
    --dataset_name argilla/Capybara-Preferences \
    --output_dir Qwen2.5-0.5B-DPO \
    --per_device_train_batch_size 4 \
    --learning_rate 5e-7 \
    --beta 0.1
```

### Рабочий процесс 3: онлайн-RL с эффективным использованием памяти с помощью GRPO

Тренируйтесь с помощью обучения с подкреплением, используя минимум памяти.

Подробное руководство GRPO — проектирование функции вознаграждения, важные сведения о обучении (поведение при потерях, коллапс режима, настройка) и расширенные многоэтапные шаблоны — см. **[references/grpo-training.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/training/trl-fine-tuning/references/grpo-training.md)**. Готовый к использованию сценарий обучения находится в **[templates/basic_grpo_training.py](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/training/trl-fine-tuning/templates/basic_grpo_training.py)**.

Скопируйте этот контрольный список:

```
GRPO Training:
- [ ] Step 1: Define reward function
- [ ] Step 2: Configure GRPO
- [ ] Step 3: Train with GRPOTrainer
```

**Шаг 1. Определите функцию вознаграждения**

```python
def reward_function(completions, **kwargs):
    """
    Compute rewards for completions.

    Args:
        completions: List of generated texts

    Returns:
        List of reward scores (floats)
    """
    rewards = []
    for completion in completions:
        # Example: reward based on length and unique words
        score = len(completion.split())  # Favor longer responses
        score += len(set(completion.lower().split()))  # Reward unique words
        rewards.append(score)
    return rewards
```

Или используйте модель вознаграждения:
```python
from transformers import pipeline

reward_model = pipeline("text-classification", model="reward-model-path")

def reward_from_model(completions, prompts, **kwargs):
    # Combine prompt + completion
    full_texts = [p + c for p, c in zip(prompts, completions)]
    # Get reward scores
    results = reward_model(full_texts)
    return [r["score"] for r in results]
```

**Шаг 2. Настройте GRPO**

```python
from trl import GRPOConfig

config = GRPOConfig(
    output_dir="Qwen2-GRPO",
    per_device_train_batch_size=4,
    num_train_epochs=1,
    learning_rate=1e-5,
    num_generations=4,  # Generate 4 completions per prompt
    max_new_tokens=128
)
```

**Шаг 3. Тренируйтесь с GRPOTrainer**

```python
from datasets import load_dataset
from trl import GRPOTrainer

# Load prompt-only dataset
dataset = load_dataset("trl-lib/tldr", split="train")

trainer = GRPOTrainer(
    model="Qwen/Qwen2-0.5B-Instruct",
    reward_funcs=reward_function,  # Your reward function
    args=config,
    train_dataset=dataset
)

trainer.train()
```

**Интерфейс командной строки**:
```bash
trl grpo \
    --model_name_or_path Qwen/Qwen2-0.5B-Instruct \
    --dataset_name trl-lib/tldr \
    --output_dir Qwen2-GRPO \
    --num_generations 4
```

## Когда использовать альтернативы

**Используйте TRL, когда:**
- Необходимо привести модель в соответствие с предпочтениями человека.
- Иметь данные о предпочтениях (выбранные/отклоненные пары)
- Хотите использовать обучение с подкреплением (RLOO, GRPO)
- Необходимо обучение модели вознаграждения
- Выполнение RLHF (полный конвейер)

**Выбор метода**:
- **SFT**: есть пары «подсказка-завершение», требуется выполнение основных инструкций.
- **DPO**: есть предпочтения, требуется простое согласование (модель вознаграждения не требуется)
- **RLOO**: есть модель вознаграждения, требуется онлайн-RL (путь RLHF, управляемый моделью вознаграждения; PPO был удален в TRL 1.x)
- **GRPO**: ограничена память, нужен онлайн-RL с функциями вознаграждения.
- **Модель вознаграждения**: создание конвейера RLHF, необходимо подсчитать количество поколений.

**Вместо этого используйте альтернативы:**
- **HuggingFace Trainer**: базовая точная настройка без RL.
- **Аксолотль**: конфигурация обучения на основе YAML.
- **LitGPT**: обучающее, минимальная тонкая настройка.
- **Unsloth**: быстрое обучение LoRA.

## Распространенные проблемы

**Проблема: OOM во время обучения DPO**

Уменьшите размер пакета и длину последовательности:
```python
config = DPOConfig(
    per_device_train_batch_size=1,  # Reduce from 4
    max_length=512,  # Reduce from 1024
    gradient_accumulation_steps=8  # Maintain effective batch
)
```

Или используйте контрольную точку градиента:
```python
model.gradient_checkpointing_enable()
```

**Проблема: плохое качество выравнивания**

Настройте бета-параметр:
```python
# Higher beta = more conservative (stays closer to reference)
config = DPOConfig(beta=0.5)  # Default 0.1

# Lower beta = more aggressive alignment
config = DPOConfig(beta=0.01)
```

**Проблема: модель вознаграждения не обучается**

Проверьте тип потери и скорость обучения:
```python
config = RewardConfig(
    learning_rate=1e-5,  # Try different LR
    num_train_epochs=3  # Train longer
)
```

Убедитесь, что в наборе данных предпочтений есть явные победители:
```python
# Verify dataset
print(dataset[0])
# Should have clear chosen > rejected
```

**Проблема: онлайн-обучение RL (RLOO/GRPO) нестабильно**

Настройте регуляризацию KL/beta в соответствии с эталонной политикой:
```python
from trl import RLOOConfig

config = RLOOConfig(
    beta=0.05,          # KL coefficient toward the reference model (increase for stability)
    num_generations=4,  # more samples per prompt = lower-variance advantage estimates
)
```

## Расширенные темы

**Руководство по обучению SFT**: см. [references/sft-training.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/training/trl-fine-tuning/references/sft-training.md) для получения информации о форматах наборов данных, шаблонах чатов, стратегиях упаковки и обучении с использованием нескольких графических процессоров.

**Варианты DPO**: см. [references/dpo-variants.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/training/trl-fine-tuning/references/dpo-variants.md) для IPO, cDPO, RPO и других функций потери DPO с рекомендуемыми гиперпараметрами.

**Моделирование вознаграждения**: см. [references/reward-modeling.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/training/trl-fine-tuning/references/reward-modeling.md) для получения информации о вознаграждениях за результат и процесс, потерях Брэдли-Терри и оценке модели вознаграждения.

**Методы онлайн-RL**: см. [references/online-rl.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/training/trl-fine-tuning/references/online-rl.md) для PPO, GRPO, RLOO и OnlineDPO с подробными настройками.

**Подробное погружение в GRPO**: шаблоны GRPO экспертного уровня см. в [references/grpo-training.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/training/trl-fine-tuning/references/grpo-training.md) — философия разработки функций вознаграждения, идеи обучения (почему увеличиваются потери, обнаружение коллапса режима), настройка гиперпараметров, многоэтапное обучение и устранение неполадок. Готовый к использованию шаблон в [templates/basic_grpo_training.py](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/training/trl-fine-tuning/templates/basic_grpo_training.py).

## Требования к оборудованию

- **Графический процессор**: NVIDIA (требуется CUDA)
- **VRAM**: зависит от модели и метода.
  - SFT 7B: 16 ГБ (с LoRA)
  - DPO 7B: 24 ГБ (хранится эталонная модель)
  - RLOO 7B: 40 ГБ (модель «полис + вознаграждение»)
  - GRPO 7B: 24 ГБ (более эффективное использование памяти)
- **Мульти-GPU**: поддерживается через `accelerate`.
- **Смешанная точность**: рекомендуется BF16 (A100/H100).

**Оптимизация памяти**:
- Используйте LoRA/QLoRA для всех методов.
- Включить контрольную точку градиента
- Используйте меньшие размеры партий с накоплением градиента.

## Ресурсы

- Документы: https://huggingface.co/docs/trl/
- GitHub: https://github.com/huggingface/trl
- Документы:
  - «Обучение языковых моделей следованию инструкциям с обратной связью от человека» (InstructGPT, 2022 г.)
  - «Прямая оптимизация предпочтений: ваша языковая модель тайно является моделью вознаграждения» (DPO, 2023 г.)
  - «Оптимизация групповой относительной политики» (ГРПО, 2024 г.)
- Примеры: https://github.com/huggingface/trl/tree/main/examples/scripts.