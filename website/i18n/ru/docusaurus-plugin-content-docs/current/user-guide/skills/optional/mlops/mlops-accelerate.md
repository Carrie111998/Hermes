---
title: Ускорение — запускайте обучение PyTorch на всех графических процессорах с минимальными
  изменениями.
sidebar_label: Accelerate
description: Run PyTorch training across GPUs with minimal changes
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Ускорение

Запускайте обучение PyTorch на всех графических процессорах с минимальными изменениями.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/mlops/accelerate` |
| Путь | `optional-skills/mlops/accelerate` |
| Версия | `1.0.1` |
| Автор | Исследование оркестра |
| Лицензия | Массачусетский технологический институт |
| Зависимости | `accelerate`, `torch`, `transformers` |
| Платформы | Linux, MacOS, Windows |
| Теги | `Distributed Training`, `HuggingFace`, `Accelerate`, `DeepSpeed`, `FSDP`, `Mixed Precision`, `PyTorch`, `DDP`, `Unified API`, `Simple` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# HuggingFace Accelerate — унифицированное распределенное обучение

## Быстрый старт

Accelerate упрощает распределенное обучение до 4 строк кода.

**Установка**:
```bash
pip install accelerate
```

**Преобразовать скрипт PyTorch** (4 строки):
```python
import torch
+ from accelerate import Accelerator

+ accelerator = Accelerator()

  model = torch.nn.Transformer()
  optimizer = torch.optim.Adam(model.parameters())
  dataloader = torch.utils.data.DataLoader(dataset)

+ model, optimizer, dataloader = accelerator.prepare(model, optimizer, dataloader)

  for batch in dataloader:
      optimizer.zero_grad()
      loss = model(batch)
-     loss.backward()
+     accelerator.backward(loss)
      optimizer.step()
```

**Выполнить** (одиночная команда):
```bash
accelerate launch train.py
```

## Общие рабочие процессы

### Рабочий процесс 1: от одного графического процессора к нескольким графическим процессорам

**Оригинальный сценарий**:
```python
# train.py
import torch

model = torch.nn.Linear(10, 2).to('cuda')
optimizer = torch.optim.Adam(model.parameters())
dataloader = torch.utils.data.DataLoader(dataset, batch_size=32)

for epoch in range(10):
    for batch in dataloader:
        batch = batch.to('cuda')
        optimizer.zero_grad()
        loss = model(batch).mean()
        loss.backward()
        optimizer.step()
```

**С ускорением** (добавлено 4 строки):
```python
# train.py
import torch
from accelerate import Accelerator  # +1

accelerator = Accelerator()  # +2

model = torch.nn.Linear(10, 2)
optimizer = torch.optim.Adam(model.parameters())
dataloader = torch.utils.data.DataLoader(dataset, batch_size=32)

model, optimizer, dataloader = accelerator.prepare(model, optimizer, dataloader)  # +3

for epoch in range(10):
    for batch in dataloader:
        # No .to('cuda') needed - automatic!
        optimizer.zero_grad()
        loss = model(batch).mean()
        accelerator.backward(loss)  # +4
        optimizer.step()
```

**Настроить** (интерактивно):
```bash
accelerate config
```

**Вопросы**:
- Какая машина? (один/несколько графических процессоров/ТПУ/ЦП)
- Сколько машин? (1)
- Смешанная точность? (нет/fp16/bf16/fp8)
- ДипСпид? (нет/да)

**Запуск** (работает при любых настройках):
```bash
# Single GPU
accelerate launch train.py

# Multi-GPU (8 GPUs)
accelerate launch --multi_gpu --num_processes 8 train.py

# Multi-node
accelerate launch --multi_gpu --num_processes 16 \
  --num_machines 2 --machine_rank 0 \
  --main_process_ip $MASTER_ADDR \
  train.py
```

### Рабочий процесс 2: тренировка смешанной точности

**Включить FP16/BF16**:
```python
from accelerate import Accelerator

# FP16 (with gradient scaling)
accelerator = Accelerator(mixed_precision='fp16')

# BF16 (no scaling, more stable)
accelerator = Accelerator(mixed_precision='bf16')

# FP8 (H100+)
accelerator = Accelerator(mixed_precision='fp8')

model, optimizer, dataloader = accelerator.prepare(model, optimizer, dataloader)

# Everything else is automatic!
for batch in dataloader:
    with accelerator.autocast():  # Optional, done automatically
        loss = model(batch)
    accelerator.backward(loss)
```

### Рабочий процесс 3: интеграция DeepSpeed ZeRO

**Включить DeepSpeed ZeRO-2** (передать `DeepSpeedPlugin`, а не необработанный запрос):
```python
from accelerate import Accelerator, DeepSpeedPlugin

deepspeed_plugin = DeepSpeedPlugin(
    zero_stage=2,                     # ZeRO-2
    offload_optimizer_device="none",  # or "cpu" to offload
    gradient_accumulation_steps=4,
)

accelerator = Accelerator(
    mixed_precision='bf16',
    deepspeed_plugin=deepspeed_plugin,  # DeepSpeedPlugin instance (or dict[str, DeepSpeedPlugin])
)

# Same code as before!
model, optimizer, dataloader = accelerator.prepare(model, optimizer, dataloader)
```

**Или укажите полную конфигурацию DeepSpeed JSON через плагин**:
```python
from accelerate import Accelerator, DeepSpeedPlugin

# hf_ds_config accepts a path to a DeepSpeed config JSON (or a dict)
deepspeed_plugin = DeepSpeedPlugin(hf_ds_config="ds_config.json")
accelerator = Accelerator(mixed_precision='bf16', deepspeed_plugin=deepspeed_plugin)
```

**ds_config.json** (необработанная конфигурация DeepSpeed — передается через плагин, НЕ через `--config_file`):
```json
{
    "fp16": {"enabled": false},
    "bf16": {"enabled": true},
    "zero_optimization": {
        "stage": 2,
        "offload_optimizer": {"device": "cpu"},
        "allgather_bucket_size": 5e8,
        "reduce_bucket_size": 5e8
    }
}
```

**Или через интерактивную настройку**:
```bash
accelerate config
# Select: DeepSpeed → ZeRO-2
# This writes an accelerate YAML config (default: ~/.cache/huggingface/accelerate/default_config.yaml)
```

**Запуск** (`--config_file` ожидает ускоренного YAML, а не необработанного DeepSpeed JSON):
```bash
# Uses the default accelerate config written by `accelerate config`
accelerate launch train.py

# Or point at a specific accelerate YAML
accelerate launch --config_file accelerate_deepspeed.yaml train.py
```

### Рабочий процесс 4: FSDP (полностью сегментированный параллелизм данных)

**Включить ФСДП**:
```python
from accelerate import Accelerator, FullyShardedDataParallelPlugin

fsdp_plugin = FullyShardedDataParallelPlugin(
    sharding_strategy="FULL_SHARD",  # ZeRO-3 equivalent
    auto_wrap_policy="transformer_based_wrap",  # valid: transformer_based_wrap | size_based_wrap | no_wrap
    cpu_offload=False
)

accelerator = Accelerator(
    mixed_precision='bf16',
    fsdp_plugin=fsdp_plugin
)

model, optimizer, dataloader = accelerator.prepare(model, optimizer, dataloader)
```

**Или через конфигурацию**:
```bash
accelerate config
# Select: FSDP → Full Shard → No CPU Offload
```

### Рабочий процесс 5: накопление градиента

**Накопление градиентов**:
```python
from accelerate import Accelerator

accelerator = Accelerator(gradient_accumulation_steps=4)

model, optimizer, dataloader = accelerator.prepare(model, optimizer, dataloader)

for batch in dataloader:
    with accelerator.accumulate(model):  # Handles accumulation
        optimizer.zero_grad()
        loss = model(batch)
        accelerator.backward(loss)
        optimizer.step()
```

**Эффективный размер пакета**: `batch_size * num_gpus * gradient_accumulation_steps`

## Когда использовать альтернативы

**Используйте ускорение, когда**:
- Хотите простейшее распределенное обучение
- Нужен единый скрипт для любого оборудования
- Используйте экосистему HuggingFace
- Хотите гибкости (DDP/DeepSpeed/FSDP/Megatron)
- Нужно быстрое прототипирование

**Основные преимущества**:
- **4 строки**: минимальные изменения кода.
- **Единый API**: один и тот же код для DDP, DeepSpeed, FSDP, Megatron.
- **Автоматически**: размещение устройства, смешанная точность, сегментирование.
- **Интерактивная конфигурация**: нет ручной настройки лаунчера.
- **Один запуск**: работает везде.

**Вместо этого используйте альтернативы**:
- **PyTorch Lightning**: нужны обратные вызовы, абстракции высокого уровня.
- **Ray Train**: многоузловая оркестровка, настройка гиперпараметров.
- **DeepSpeed**: прямое управление через API, расширенные функции.
- **Raw DDP**: максимальный контроль, минимальная абстракция.

## Распространенные проблемы

**Проблема: неправильное размещение устройства**

Не перемещайтесь на устройство вручную:
```python
# WRONG
batch = batch.to('cuda')

# CORRECT
# Accelerate handles it automatically after prepare()
```

**Проблема: накопление градиента не работает**

Используйте контекстный менеджер:
```python
# CORRECT
with accelerator.accumulate(model):
    optimizer.zero_grad()
    accelerator.backward(loss)
    optimizer.step()
```

**Проблема: контрольные точки в распределенном режиме**

Используйте методы-ускорители:
```python
# Save only on main process
if accelerator.is_main_process:
    accelerator.save_state('checkpoint/')

# Load on all processes
accelerator.load_state('checkpoint/')
```

**Проблема: разные результаты при использовании FSDP**

Обеспечьте такое же случайное начальное число:
```python
from accelerate.utils import set_seed
set_seed(42)
```

## Расширенные темы

**Интеграция Megatron**: см. [references/megatron-integration.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/accelerate/references/megatron-integration.md) для получения информации о тензорном параллелизме, конвейерном параллелизме и настройке параллелизма последовательностей.

**Пользовательские плагины**: см. [references/custom-plugins.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/accelerate/references/custom-plugins.md) для создания пользовательских распределенных плагинов и расширенной настройки.

**Настройка производительности**. См. [references/ Performance.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/accelerate/references/ Performance.md) для профилирования, оптимизации памяти и рекомендаций.

## Требования к оборудованию

- **ЦП**: Работает (медленно)
- **Один графический процессор**: работает
- **Мульти-GPU**: DDP (по умолчанию), DeepSpeed или FSDP.
- **Многоузловая**: DDP, DeepSpeed, FSDP, Megatron.
- **ТПУ**: поддерживается
- **Apple MPS**: поддерживается.

**Требования к лаунчеру**:
- **DDP**: `torch.distributed.run` (встроенный)
- **DeepSpeed**: `deepspeed` (pip install deepspeed)
- **FSDP**: PyTorch 1.12+ (встроенный)
- **Мегатрон**: индивидуальная настройка.

## Ресурсы

- Документы: https://huggingface.co/docs/accelerate.
- GitHub: https://github.com/huggingface/accelerate
- Версия: 1.11.0+
- Учебное пособие: «Ускорьте свои скрипты»
- Примеры: https://github.com/huggingface/accelerate/tree/main/examples.
- Используется: HuggingFace Transformers, TRL, PEFT, всеми библиотеками HF.