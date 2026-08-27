---
title: Peft — точная настройка больших LLM с помощью LoRA на ограниченной памяти графического
  процессора.
sidebar_label: Peft
description: Точная настройка больших LLM с помощью LoRA на ограниченной памяти графического
  процессора
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Пефт

Точная настройка больших LLM с помощью LoRA на ограниченной памяти графического процессора.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/mlops/peft` |
| Путь | `optional-skills/mlops/peft` |
| Версия | `1.0.0` |
| Автор | Исследование оркестра |
| Лицензия | Массачусетский технологический институт |
| Зависимости | `peft>=0.13.0`, `transformers>=4.45.0`, `torch>=2.0.0`, `bitsandbytes>=0.43.0` |
| Платформы | Linux, MacOS, Windows |
| Теги | `Fine-Tuning`, `PEFT`, `LoRA`, `QLoRA`, `Parameter-Efficient`, `Adapters`, `Low-Rank`, `Memory Optimization`, `Multi-Adapter` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# PEFT (Точная настройка с эффективным использованием параметров)

Точная настройка LLM путем обучения &lt;1% параметров с использованием методов LoRA, QLoRA и более 25 адаптеров.

## Когда использовать PEFT

**Используйте PEFT/LoRA, когда:**
- Точная настройка моделей 7B-70B на потребительских графических процессорах (RTX 4090, A100)
- Необходимо обучить параметры &lt;1% (адаптеры на 6 МБ против полной модели на 14 ГБ)
- Хотите быструю итерацию с несколькими адаптерами для конкретных задач
- Развертывание нескольких доработанных вариантов одной базовой модели.

**Используйте QLoRA (PEFT + квантование), когда:**
- Точная настройка моделей 70B на одном графическом процессоре емкостью 24 ГБ.
- Память является основным ограничением
- Можно принять компромисс качества ~5% по сравнению с полной тонкой настройкой

**Вместо этого используйте полную точную настройку, если:**
- Обучение небольших моделей (параметры &lt;1B)
- Нужно максимальное качество и иметь вычислительный бюджет
- Значительный сдвиг домена требует обновления всех весов.

## Быстрый старт

### Установка

```bash
# Basic installation
pip install peft

# With quantization support (recommended)
pip install peft bitsandbytes

# Full stack
pip install peft transformers accelerate bitsandbytes datasets
```

### Тонкая настройка LoRA (стандартно)

```python
from transformers import AutoModelForCausalLM, AutoTokenizer, TrainingArguments, Trainer
from peft import get_peft_model, LoraConfig, TaskType
from datasets import load_dataset

# Load base model
model_name = "meta-llama/Llama-3.1-8B"
model = AutoModelForCausalLM.from_pretrained(model_name, torch_dtype="auto", device_map="auto")
tokenizer = AutoTokenizer.from_pretrained(model_name)
tokenizer.pad_token = tokenizer.eos_token

# LoRA configuration
lora_config = LoraConfig(
    task_type=TaskType.CAUSAL_LM,
    r=16,                          # Rank (8-64, higher = more capacity)
    lora_alpha=32,                 # Scaling factor (typically 2*r)
    lora_dropout=0.05,             # Dropout for regularization
    target_modules=["q_proj", "v_proj", "k_proj", "o_proj"],  # Attention layers
    bias="none"                    # Don't train biases
)

# Apply LoRA
model = get_peft_model(model, lora_config)
model.print_trainable_parameters()
# Output: trainable params: 13,631,488 || all params: 8,043,307,008 || trainable%: 0.17%

# Prepare dataset
dataset = load_dataset("databricks/databricks-dolly-15k", split="train")

def tokenize(example):
    text = f"### Instruction:\n{example['instruction']}\n\n### Response:\n{example['response']}"
    return tokenizer(text, truncation=True, max_length=512, padding="max_length")

tokenized = dataset.map(tokenize, remove_columns=dataset.column_names)

# Training
training_args = TrainingArguments(
    output_dir="./lora-llama",
    num_train_epochs=3,
    per_device_train_batch_size=4,
    gradient_accumulation_steps=4,
    learning_rate=2e-4,
    fp16=True,
    logging_steps=10,
    save_strategy="epoch"
)

trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=tokenized,
    data_collator=lambda data: {"input_ids": torch.stack([f["input_ids"] for f in data]),
                                 "attention_mask": torch.stack([f["attention_mask"] for f in data]),
                                 "labels": torch.stack([f["input_ids"] for f in data])}
)

trainer.train()

# Save adapter only (6MB vs 16GB)
model.save_pretrained("./lora-llama-adapter")
```

### Тонкая настройка QLoRA (эффективное использование памяти)

```python
from transformers import AutoModelForCausalLM, BitsAndBytesConfig
from peft import get_peft_model, LoraConfig, prepare_model_for_kbit_training

# 4-bit quantization config
bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type="nf4",           # NormalFloat4 (best for LLMs)
    bnb_4bit_compute_dtype="bfloat16",   # Compute in bf16
    bnb_4bit_use_double_quant=True       # Nested quantization
)

# Load quantized model
model = AutoModelForCausalLM.from_pretrained(
    "meta-llama/Llama-3.1-70B",
    quantization_config=bnb_config,
    device_map="auto"
)

# Prepare for training (enables gradient checkpointing)
model = prepare_model_for_kbit_training(model)

# LoRA config for QLoRA
lora_config = LoraConfig(
    r=64,                              # Higher rank for 70B
    lora_alpha=128,
    lora_dropout=0.1,
    target_modules=["q_proj", "v_proj", "k_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    bias="none",
    task_type="CAUSAL_LM"
)

model = get_peft_model(model, lora_config)
# 70B model now fits on single 24GB GPU!
```

## Выбор параметра LoRA

### Ранг (r) — мощность против эффективности

| Ранг | Обучаемые параметры | Память | Качество | Вариант использования |
|------|-----------------|--------|---------|----------|
| 4 | ~3 млн | Минимальный | Нижний | Простые задачи, прототипирование |
| **8** | ~7 млн ​​| Низкий | Хорошо | **Рекомендуемая отправная точка** |
| **16** | ~14 млн | Средний | Лучше | **Общая точная настройка** |
| 32 | ~27 млн ​​| Высшее | Высокий | Сложные задачи |
| 64 | ~54 млн | Высокий | Самый высокий | Адаптация домена, модели 70B |

###Альфа (lora_alpha) — коэффициент масштабирования

```python
# Rule of thumb: alpha = 2 * rank
LoraConfig(r=16, lora_alpha=32)  # Standard
LoraConfig(r=16, lora_alpha=16)  # Conservative (lower learning rate effect)
LoraConfig(r=16, lora_alpha=64)  # Aggressive (higher learning rate effect)
```

### Целевые модули по архитектуре

```python
# Llama / Mistral / Qwen
target_modules = ["q_proj", "v_proj", "k_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]

# GPT-2 / GPT-Neo
target_modules = ["c_attn", "c_proj", "c_fc"]

# Falcon
target_modules = ["query_key_value", "dense", "dense_h_to_4h", "dense_4h_to_h"]

# BLOOM
target_modules = ["query_key_value", "dense", "dense_h_to_4h", "dense_4h_to_h"]

# Auto-detect all linear layers
target_modules = "all-linear"  # PEFT 0.6.0+
```

## Загрузка и объединение адаптеров

### Загрузка обученного адаптера

```python
from peft import PeftModel, AutoPeftModelForCausalLM
from transformers import AutoModelForCausalLM

# Option 1: Load with PeftModel
base_model = AutoModelForCausalLM.from_pretrained("meta-llama/Llama-3.1-8B")
model = PeftModel.from_pretrained(base_model, "./lora-llama-adapter")

# Option 2: Load directly (recommended)
model = AutoPeftModelForCausalLM.from_pretrained(
    "./lora-llama-adapter",
    device_map="auto"
)
```

### Объединить адаптер с базовой моделью

```python
# Merge for deployment (no adapter overhead)
merged_model = model.merge_and_unload()

# Save merged model
merged_model.save_pretrained("./llama-merged")
tokenizer.save_pretrained("./llama-merged")

# Push to Hub
merged_model.push_to_hub("username/llama-finetuned")
```

### Обслуживание нескольких адаптеров

```python
from peft import PeftModel

# Load base with first adapter
model = AutoPeftModelForCausalLM.from_pretrained("./adapter-task1")

# Load additional adapters
model.load_adapter("./adapter-task2", adapter_name="task2")
model.load_adapter("./adapter-task3", adapter_name="task3")

# Switch between adapters at runtime
model.set_adapter("task1")  # Use task1 adapter
output1 = model.generate(**inputs)

model.set_adapter("task2")  # Switch to task2
output2 = model.generate(**inputs)

# Disable adapters (use base model)
with model.disable_adapter():
    base_output = model.generate(**inputs)
```

## Сравнение методов PEFT

| Метод | Обучаемый % | Память | Скорость | Лучшее для |
|--------|------------|--------|-------|----------|
| **ЛОРА** | 0,1-1% | Низкий | Быстро | Общая тонкая настройка |
| **QLoRA** | 0,1-1% | Очень низкий | Средний | Ограниченная память |
| АдаЛОРА | 0,1-1% | Низкий | Средний | Автоматический выбор ранга |
| IA3 | 0,01% | Минимальный | Самый быстрый | Малокадровая адаптация |
| Приставка Тюнинг | 0,1% | Низкий | Средний | Контроль генерации |
| Оперативная настройка | 0,001% | Минимальный | Быстро | Простая адаптация задачи |
| P-Тюнинг v2 | 0,1% | Низкий | Средний | Задачи НЛУ |

###IA3 (минимальные параметры)

```python
from peft import IA3Config

ia3_config = IA3Config(
    target_modules=["q_proj", "v_proj", "k_proj", "down_proj"],
    feedforward_modules=["down_proj"]
)
model = get_peft_model(model, ia3_config)
# Trains only 0.01% of parameters!
```

### Настройка префикса

```python
from peft import PrefixTuningConfig

prefix_config = PrefixTuningConfig(
    task_type="CAUSAL_LM",
    num_virtual_tokens=20,      # Prepended tokens
    prefix_projection=True       # Use MLP projection
)
model = get_peft_model(model, prefix_config)
```

## Шаблоны интеграции

### С TRL (SFTTrainer)

```python
from trl import SFTTrainer, SFTConfig
from peft import LoraConfig

lora_config = LoraConfig(r=16, lora_alpha=32, target_modules="all-linear")

trainer = SFTTrainer(
    model=model,
    args=SFTConfig(output_dir="./output", max_seq_length=512),
    train_dataset=dataset,
    peft_config=lora_config,  # Pass LoRA config directly
)
trainer.train()
```

### С Аксолотлем (конфигурация YAML)

```yaml
# axolotl config.yaml
adapter: lora
lora_r: 16
lora_alpha: 32
lora_dropout: 0.05
lora_target_modules:
  - q_proj
  - v_proj
  - k_proj
  - o_proj
lora_target_linear: true  # Target all linear layers
```

### С vLLM (вывод)

```python
from vllm import LLM
from vllm.lora.request import LoRARequest

# Load base model with LoRA support
llm = LLM(model="meta-llama/Llama-3.1-8B", enable_lora=True)

# Serve with adapter
outputs = llm.generate(
    prompts,
    lora_request=LoRARequest("adapter1", 1, "./lora-adapter")
)
```

## Тесты производительности

### Использование памяти (Llama 3.1 8B)

| Метод | Память графического процессора | Обучаемые параметры |
|--------|-----------|------------------|
| Полная тонкая настройка | 60+ ГБ | 8Б (100%) |
| ЛоРА r=16 | 18 ГБ | 14М (0,17%) |
| QLoRA r=16 | 6 ГБ | 14М (0,17%) |
| IA3 | 16 ГБ | 800 тыс. (0,01%) |

### Скорость обучения (A100 80 ГБ)

| Метод | Токенов/сек | против полного FT |
|--------|-----------|------------|
| Полный FT | 2500 | 1x |
| ЛоРА | 3200 | 1,3x |
| КЛОРА | 2100 | 0,84x |

### Качество (тест MMLU)

| Модель | Полный FT | ЛоРА | КЛОРА |
|-------|---------|------|-------|
| Лама 2-7Б | 45,3 | 44,8 | 44,1 |
| Лама 2-13Б | 54,8 | 54,2 | 53,5 |

## Распространенные проблемы

###CUDA OOM во время тренировки

```python
# Solution 1: Enable gradient checkpointing
model.gradient_checkpointing_enable()

# Solution 2: Reduce batch size + increase accumulation
TrainingArguments(
    per_device_train_batch_size=1,
    gradient_accumulation_steps=16
)

# Solution 3: Use QLoRA
from transformers import BitsAndBytesConfig
bnb_config = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4")
```

### Адаптер не применяется

```python
# Verify adapter is active
print(model.active_adapters)  # Should show adapter name

# Check trainable parameters
model.print_trainable_parameters()

# Ensure model in training mode
model.train()
```

### Ухудшение качества

```python
# Increase rank
LoraConfig(r=32, lora_alpha=64)

# Target more modules
target_modules = "all-linear"

# Use more training data and epochs
TrainingArguments(num_train_epochs=5)

# Lower learning rate
TrainingArguments(learning_rate=1e-4)
```

## Лучшие практики

1. **Начните с r=8–16**, увеличивайте, если качество недостаточно.
2. **Используйте альфа = 2 * ранг** в качестве отправной точки.
3. **Привлечение внимания + уровни MLP** для лучшего качества и эффективности.
4. **Включите контрольную точку градиента** для экономии памяти.
5. **Часто сохраняйте адаптеры** (небольшие файлы, простой откат)
6. **Оцените имеющиеся данные** перед объединением
7. **Используйте QLoRA для моделей 70B+** на потребительском оборудовании.

## Ссылки

- **[Расширенное использование](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/peft/references/advanced-usage.md)** - DoRA, LoftQ, стабилизация ранга, пользовательские модули
- **[Устранение неполадок](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/peft/references/troubleshooting.md)** - Распространенные ошибки, отладка, оптимизация

## Ресурсы

- **GitHub**: https://github.com/huggingface/peft
- **Документация**: https://huggingface.co/docs/peft.
- **Бумага LoRA**: arXiv:2106.09685
- **Бумага QLoRA**: arXiv:2305.14314
- **Модели**: https://huggingface.co/models?library=peft