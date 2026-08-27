---
title: Pytorch Lightning — чистые циклы обучения со встроенной распределенной поддержкой.
sidebar_label: Pytorch Lightning
description: Чистые циклы обучения со встроенной распределенной поддержкой
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Молния Пайторча

Чистые циклы обучения со встроенной распределенной поддержкой.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/mlops/pytorch-lightning` |
| Путь | `optional-skills/mlops/pytorch-lightning` |
| Версия | `1.0.0` |
| Автор | Исследование оркестра |
| Лицензия | Массачусетский технологический институт |
| Зависимости | `lightning`, `torch`, `transformers` |
| Платформы | Linux, MacOS, Windows |
| Теги | `PyTorch Lightning`, `Training Framework`, `Distributed Training`, `DDP`, `FSDP`, `DeepSpeed`, `High-Level API`, `Callbacks`, `Best Practices`, `Scalable` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# PyTorch Lightning — платформа обучения высокого уровня

## Быстрый старт

PyTorch Lightning организует код PyTorch, устраняя шаблонность и сохраняя при этом гибкость.

**Установка**:
```bash
pip install lightning
```

**Преобразование PyTorch в Lightning** (3 шага):

```python
import lightning as L
import torch
from torch import nn
from torch.utils.data import DataLoader, Dataset

# Step 1: Define LightningModule (organize your PyTorch code)
class LitModel(L.LightningModule):
    def __init__(self, hidden_size=128):
        super().__init__()
        self.model = nn.Sequential(
            nn.Linear(28 * 28, hidden_size),
            nn.ReLU(),
            nn.Linear(hidden_size, 10)
        )

    def training_step(self, batch, batch_idx):
        x, y = batch
        y_hat = self.model(x)
        loss = nn.functional.cross_entropy(y_hat, y)
        self.log('train_loss', loss)  # Auto-logged to TensorBoard
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-3)

# Step 2: Create data
train_loader = DataLoader(train_dataset, batch_size=32)

# Step 3: Train with Trainer (handles everything else!)
trainer = L.Trainer(max_epochs=10, accelerator='gpu', devices=2)
model = LitModel()
trainer.fit(model, train_loader)
```

**Вот и все!** Тренер обрабатывает:
- Переключение графического процессора/ТПУ/ЦП
- Распределенное обучение (DDP, FSDP, DeepSpeed)
- Смешанная точность (FP16, BF16)
- Накопление градиента
- Контрольно-пропускной пункт
- Ведение журнала
- Индикаторы прогресса

## Общие рабочие процессы

### Рабочий процесс 1: от PyTorch к Lightning

**Исходный код PyTorch**:
```python
model = MyModel()
optimizer = torch.optim.Adam(model.parameters())
model.to('cuda')

for epoch in range(max_epochs):
    for batch in train_loader:
        batch = batch.to('cuda')
        optimizer.zero_grad()
        loss = model(batch)
        loss.backward()
        optimizer.step()
```

**Версия Lightning**:
```python
class LitModel(L.LightningModule):
    def __init__(self):
        super().__init__()
        self.model = MyModel()

    def training_step(self, batch, batch_idx):
        loss = self.model(batch)  # No .to('cuda') needed!
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters())

# Train
trainer = L.Trainer(max_epochs=10, accelerator='gpu')
trainer.fit(LitModel(), train_loader)
```

**Преимущества**: более 40 строк → 15 строк, отсутствие управления устройствами, автоматическое распределение

### Рабочий процесс 2: Проверка и тестирование

```python
class LitModel(L.LightningModule):
    def __init__(self):
        super().__init__()
        self.model = MyModel()

    def training_step(self, batch, batch_idx):
        x, y = batch
        y_hat = self.model(x)
        loss = nn.functional.cross_entropy(y_hat, y)
        self.log('train_loss', loss)
        return loss

    def validation_step(self, batch, batch_idx):
        x, y = batch
        y_hat = self.model(x)
        val_loss = nn.functional.cross_entropy(y_hat, y)
        acc = (y_hat.argmax(dim=1) == y).float().mean()
        self.log('val_loss', val_loss)
        self.log('val_acc', acc)

    def test_step(self, batch, batch_idx):
        x, y = batch
        y_hat = self.model(x)
        test_loss = nn.functional.cross_entropy(y_hat, y)
        self.log('test_loss', test_loss)

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-3)

# Train with validation
trainer = L.Trainer(max_epochs=10)
trainer.fit(model, train_loader, val_loader)

# Test
trainer.test(model, test_loader)
```

**Автоматические функции**:
- Проверка выполняется каждую эпоху по умолчанию.
- Метрики регистрируются в TensorBoard.
- Лучшая модель контрольной точки на основе val_loss

### Рабочий процесс 3: Распределенное обучение (DDP)

```python
# Same code as single GPU!
model = LitModel()

# 8 GPUs with DDP (automatic!)
trainer = L.Trainer(
    accelerator='gpu',
    devices=8,
    strategy='ddp'  # Or 'fsdp', 'deepspeed'
)

trainer.fit(model, train_loader)
```

**Запуск**:
```bash
# Single command, Lightning handles the rest
python train.py
```

**Изменений не требуется**:
- Автоматическое распространение данных
- Синхронизация градиента
- Поддержка нескольких узлов (просто установите `num_nodes=2`)

### Рабочий процесс 4: обратные вызовы для мониторинга

```python
from lightning.pytorch.callbacks import ModelCheckpoint, EarlyStopping, LearningRateMonitor

# Create callbacks
checkpoint = ModelCheckpoint(
    monitor='val_loss',
    mode='min',
    save_top_k=3,
    filename='model-{epoch:02d}-{val_loss:.2f}'
)

early_stop = EarlyStopping(
    monitor='val_loss',
    patience=5,
    mode='min'
)

lr_monitor = LearningRateMonitor(logging_interval='epoch')

# Add to Trainer
trainer = L.Trainer(
    max_epochs=100,
    callbacks=[checkpoint, early_stop, lr_monitor]
)

trainer.fit(model, train_loader, val_loader)
```

**Результат**:
- Автоматически сохраняет 3 лучшие модели
- Останавливается раньше, если нет улучшений в течение 5 эпох.
- Регистрирует скорость обучения в TensorBoard.

### Рабочий процесс 5: планирование скорости обучения

```python
class LitModel(L.LightningModule):
    # ... (training_step, etc.)

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=1e-3)

        # Cosine annealing
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
            optimizer,
            T_max=100,
            eta_min=1e-5
        )

        return {
            'optimizer': optimizer,
            'lr_scheduler': {
                'scheduler': scheduler,
                'interval': 'epoch',  # Update per epoch
                'frequency': 1
            }
        }

# Learning rate auto-logged!
trainer = L.Trainer(max_epochs=100)
trainer.fit(model, train_loader)
```

## Когда использовать альтернативы

**Используйте PyTorch Lightning, когда**:
- Хотите чистый, организованный код
- Нужны готовые к использованию циклы обучения.
- Переключение между одним графическим процессором, несколькими графическими процессорами, TPU
- Хотите встроенные обратные вызовы и журналирование
- Взаимодействие в команде (стандартизированная структура)

**Основные преимущества**:
- **Организованно**: отделяет исследовательский код от инженерного.
- **Автоматический**: DDP, FSDP, DeepSpeed с 1 линией
– **Обратные вызовы**: модульные расширения обучения.
- **Воспроизводимость**: меньше шаблонов = меньше ошибок.
- **Протестировано**: более 1 млн загрузок в месяц, проверено в боевых условиях.

**Вместо этого используйте альтернативы**:
- **Ускорение**: минимальные изменения в существующем коде, большая гибкость.
- **Ray Train**: многоузловая оркестровка, настройка гиперпараметров.
- **Raw PyTorch**: максимальный контроль, цели обучения.
- **Keras**: экосистема TensorFlow.

## Распространенные проблемы

**Проблема: потери не уменьшаются**

Проверьте данные и настройку модели:
```python
# Add to training_step
def training_step(self, batch, batch_idx):
    if batch_idx == 0:
        print(f"Batch shape: {batch[0].shape}")
        print(f"Labels: {batch[1]}")
    loss = ...
    return loss
```

**Проблема: недостаточно памяти**

Уменьшите размер пакета или используйте накопление градиента:
```python
trainer = L.Trainer(
    accumulate_grad_batches=4,  # Effective batch = batch_size × 4
    precision='bf16'  # Or 'fp16', reduces memory 50%
)
```

**Проблема: проверка не выполняется**

Убедитесь, что вы передали val_loader:
```python
# WRONG
trainer.fit(model, train_loader)

# CORRECT
trainer.fit(model, train_loader, val_loader)
```

**Проблема: DDP неожиданно запускает несколько процессов**

Lightning автоматически определяет графические процессоры. Явно заданные устройства:
```python
# Test on CPU first
trainer = L.Trainer(accelerator='cpu', devices=1)

# Then GPU
trainer = L.Trainer(accelerator='gpu', devices=1)
```

## Расширенные темы

**Обратные вызовы**: см. [references/callbacks.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/pytorch-lightning/references/callbacks.md) для получения информации о EarlyStopping, ModelCheckpoint, пользовательских обратных вызовах и перехватчиках обратного вызова.

**Распределенные стратегии**: см. [references/distributed.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/pytorch-lightning/references/distributed.md) для DDP, FSDP, интеграции DeepSpeed ​​ZeRO, настройки нескольких узлов.

**Настройка гиперпараметров**: см. [references/hyperparameter-tuning.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/pytorch-lightning/references/hyperparameter-tuning.md) для интеграции с развертками Optuna, Ray Tune и WandB.

## Требования к оборудованию

- **ЦП**: работает (хорошо для отладки)
- **Один графический процессор**: работает
- **Мульти-GPU**: DDP (по умолчанию), FSDP или DeepSpeed.
- **Многоузловая**: DDP, FSDP, DeepSpeed
- **ТПУ**: поддерживается (8 ядер).
- **Apple MPS**: поддерживается.

**Параметры точности**:
- FP32 (по умолчанию)
- FP16 (V100, старые графические процессоры)
- BF16 (рекомендуется A100/H100)
- ФП8 (Н100)

## Ресурсы

- Документы: https://lightning.ai/docs/pytorch/stable/
- GitHub: https://github.com/Lightning-AI/pytorch-lightning ⭐ 29 000+
- Версия: 2.5.5+
- Примеры: https://github.com/Lightning-AI/pytorch-lightning/tree/master/examples.
- Дискорд: https://discord.gg/lightning-ai
- Используется: победителями Kaggle, исследовательскими лабораториями, производственными группами.