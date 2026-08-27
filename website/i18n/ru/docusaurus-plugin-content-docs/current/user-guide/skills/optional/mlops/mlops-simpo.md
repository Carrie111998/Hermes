---
title: Simpo — выравнивание предпочтений без ссылок, проще, чем DPO.
sidebar_label: Simpo
description: Выравнивание предпочтений без ссылок, проще, чем DPO
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Симпо

Выравнивание предпочтений без ссылок, проще, чем DPO.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/mlops/simpo` |
| Путь | `optional-skills/mlops/simpo` |
| Версия | `1.0.0` |
| Автор | Исследование оркестра |
| Лицензия | Массачусетский технологический институт |
| Зависимости | `torch`, `transformers`, `datasets`, `trl`, `accelerate` |
| Платформы | Linux, MacOS, Windows |
| Теги | `Post-Training`, `SimPO`, `Preference Optimization`, `Alignment`, `DPO Alternative`, `Reference-Free`, `LLM Alignment`, `Efficient Training` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# SimPO — Простая оптимизация предпочтений

## Быстрый старт

SimPO — это метод оптимизации предпочтений без ссылок, который превосходит DPO без необходимости использования эталонной модели.

**Установка**:
```bash
# Create environment
conda create -n simpo python=3.10 && conda activate simpo

# Install PyTorch 2.2.2
# Visit: https://pytorch.org/get-started/locally/

# Install alignment-handbook
git clone https://github.com/huggingface/alignment-handbook.git
cd alignment-handbook
python -m pip install .

# Install Flash Attention 2
python -m pip install flash-attn --no-build-isolation
```

**Тренинг** (Мистраль 7Б):
```bash
ACCELERATE_LOG_LEVEL=info accelerate launch \
  --config_file accelerate_configs/deepspeed_zero3.yaml \
  scripts/run_simpo.py \
  training_configs/mistral-7b-base-simpo.yaml
```

## Общие рабочие процессы

### Рабочий процесс 1: Поезд из базовой модели (Мистраль 7Б)

**Конфигурация** (`mistral-7b-base-simpo.yaml`):
```yaml
# Model
model_name_or_path: mistralai/Mistral-7B-v0.1
torch_dtype: bfloat16

# Dataset
dataset_mixer:
  HuggingFaceH4/ultrafeedback_binarized: 1.0
dataset_splits:
  - train_prefs
  - test_prefs

# SimPO hyperparameters
beta: 2.0                  # Reward scaling (2.0-10.0)
gamma_beta_ratio: 0.5       # Target margin (0-1)
loss_type: sigmoid          # sigmoid or hinge
sft_weight: 0.0             # Optional SFT regularization

# Training
learning_rate: 5e-7         # Critical: 3e-7 to 1e-6
num_train_epochs: 1
per_device_train_batch_size: 1
gradient_accumulation_steps: 8

# Output
output_dir: ./outputs/mistral-7b-simpo
```

**Запуск обучения**:
```bash
accelerate launch --config_file accelerate_configs/deepspeed_zero3.yaml \
  scripts/run_simpo.py training_configs/mistral-7b-base-simpo.yaml
```

### Рабочий процесс 2: точная настройка модели инструкций (Llama 3 8B)

**Конфигурация** (`llama3-8b-instruct-simpo.yaml`):
```yaml
model_name_or_path: meta-llama/Meta-Llama-3-8B-Instruct

dataset_mixer:
  argilla/ultrafeedback-binarized-preferences-cleaned: 1.0

beta: 2.5
gamma_beta_ratio: 0.5
learning_rate: 5e-7
sft_weight: 0.1             # Add SFT loss to preserve capabilities

num_train_epochs: 1
per_device_train_batch_size: 2
gradient_accumulation_steps: 4
output_dir: ./outputs/llama3-8b-simpo
```

**Запуск**:
```bash
accelerate launch --config_file accelerate_configs/deepspeed_zero3.yaml \
  scripts/run_simpo.py training_configs/llama3-8b-instruct-simpo.yaml
```

### Рабочий процесс 3: Задачи, требующие интенсивного рассуждения (нижний LR)

**Для задач по математике и кодированию**:
```yaml
model_name_or_path: deepseek-ai/deepseek-math-7b-base

dataset_mixer:
  argilla/distilabel-math-preference-dpo: 1.0

beta: 5.0                   # Higher for stronger signal
gamma_beta_ratio: 0.7       # Larger margin
learning_rate: 3e-7         # Lower LR for reasoning
sft_weight: 0.0

num_train_epochs: 1
per_device_train_batch_size: 1
gradient_accumulation_steps: 16
```

## Когда использовать альтернативы

**Используйте SimPO, когда**:
- Хотите более простое обучение, чем DPO (без эталонной модели)
- Иметь данные о предпочтениях (выбранные/отклоненные пары)
- Нужна более высокая производительность, чем у DPO
- Ограниченные вычислительные ресурсы
- Достаточно обучения на одном узле

**Выбор алгоритма**:
- **SimPO**: самый простой, лучшая производительность, без эталонной модели.
- **DPO**: требуется базовая модель эталонной модели, более консервативная.
- **PPO**: максимальный контроль, необходима модель вознаграждения, сложная настройка.
- **GRPO**: RL с эффективным использованием памяти, без критики

**Вместо этого используйте альтернативы**:
- **OpenRLHF**: многоузловое распределенное обучение, PPO/GRPO.
- **TRL**: необходимо несколько методов в одной структуре.
- **DPO**: установленное базовое сравнение.

## Распространенные проблемы

**Проблема: расхождение убытков**

Уменьшите скорость обучения:
```yaml
learning_rate: 3e-7  # Reduce from 5e-7
```

Уменьшить бета:
```yaml
beta: 1.0  # Reduce from 2.0
```

**Проблема: модель забывает возможности**

Добавьте регуляризацию SFT:
```yaml
sft_weight: 0.1  # Add SFT loss component
```

**Проблема: плохое разделение предпочтений**

Увеличение бета и маржи:
```yaml
beta: 5.0            # Increase from 2.0
gamma_beta_ratio: 0.8  # Increase from 0.5
```

**Проблема: ООМ во время тренировки**

Уменьшить размер партии:
```yaml
per_device_train_batch_size: 1
gradient_accumulation_steps: 16  # Maintain effective batch
```

Включите контрольную точку градиента:
```yaml
gradient_checkpointing: true
```

## Расширенные темы

**Функции потерь**: см. [references/loss-functions.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/simpo/references/loss-functions.md) для получения информации о сигмовидной и шарнирной потере, математических формулировках и о том, когда использовать каждую из них.

**Настройка гиперпараметров**: см. [references/hyperparameters.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/simpo/references/hyperparameters.md) для ознакомления с бета-версией, гаммой, руководством по выбору скорости обучения и рекомендациями для конкретных размеров модели.

**Подготовка набора данных**: см. [references/datasets.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/simpo/references/datasets.md) для получения информации о предпочтительных форматах данных, фильтрации качества и создании пользовательских наборов данных.

## Требования к оборудованию

- **Графический процессор**: рекомендуется NVIDIA A100/H100.
- **ВОЗУ**:
  - Модель 7B: 1 × A100 40 ГБ (DeepSpeed ZeRO-3)
  - Модель 8B: 2 × A100 40 ГБ
  - Модель 70B: 8 × A100 80 ГБ
- **Один узел**: достаточно DeepSpeed ZeRO-3
- **Смешанная точность**: рекомендуется BF16.

**Оптимизация памяти**:
- DeepSpeed ZeRO-3 (конфигурация по умолчанию)
- Контрольная точка градиента
- Вспышка внимания 2

## Ресурсы

- Статья: https://arxiv.org/abs/2405.14734 (NeurIPS 2024).
- GitHub: https://github.com/princeton-nlp/SimPO
- Модели: https://huggingface.co/princeton-nlp
- Справочник по выравниванию: https://github.com/huggingface/alignment-handbook.