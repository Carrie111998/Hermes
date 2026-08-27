---
title: Lambda Labs — облачные экземпляры графического процессора по требованию для
  обучения машинному обучению
sidebar_label: Lambda Labs
description: Облачные экземпляры графического процессора по требованию для обучения
  машинному обучению
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Лямбда-лаборатории

Облачные экземпляры графического процессора по требованию для обучения машинному обучению.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/mlops/lambda-labs` |
| Путь | `optional-skills/mlops/lambda-labs` |
| Версия | `1.0.0` |
| Автор | Исследование оркестра |
| Лицензия | Массачусетский технологический институт |
| Зависимости | `lambda-cloud-client>=1.0.0` |
| Платформы | Linux, MacOS, Windows |
| Теги | `Infrastructure`, `GPU Cloud`, `Training`, `Inference`, `Lambda Labs` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Облако графических процессоров Lambda Labs

Руководство по запуску рабочих нагрузок машинного обучения в облаке графических процессоров Lambda Labs с экземплярами по требованию и кластерами в один клик.

## Когда использовать Lambda Labs

**Используйте Lambda Labs, когда:**
- Нужны выделенные экземпляры графического процессора с полным доступом по SSH.
- Выполнение длительных обучающих работ (от часов до дней)
- Хотите простое ценообразование без платы за выход
- Необходимо постоянное хранилище между сеансами
- Требуются высокопроизводительные многоузловые кластеры (16–512 графических процессоров).
- Хотите предустановленный стек ML (Lambda Stack с PyTorch, CUDA, NCCL)

**Основные особенности:**
- **Разновидность графического процессора**: B200, H100, GH200, A100, A10, A6000, V100.
- **Лямбда-стек**: предустановленные PyTorch, TensorFlow, CUDA, cuDNN, NCCL.
- **Постоянные файловые системы**: данные сохраняются при перезапуске экземпляра.
- **Кластеры в один клик**: кластеры Slurm на 16–512 графических процессоров с InfiniBand.
- **Простые цены**: поминутная оплата, плата за исходящий трафик отсутствует.
- **Глобальные регионы**: более 12 регионов по всему миру.

**Вместо этого используйте альтернативы:**
- **Модальный**: для бессерверных рабочих нагрузок с автоматическим масштабированием.
- **SkyPilot**: для оркестровки нескольких облаков и оптимизации затрат.
- **RunPod**: для более дешевых спотовых инстансов и бессерверных конечных точек.
- **Vast.ai**: рынок графических процессоров с самыми низкими ценами.

## Быстрый старт

### Настройка учетной записи

1. Создайте аккаунт на https://lambda.ai.
2. Добавьте способ оплаты
3. Сгенерируйте ключ API с панели управления.
4. Добавьте ключ SSH (требуется перед запуском инстансов)

### Запуск через консоль

1. Перейдите на https://cloud.lambda.ai/instances.
2. Нажмите «Запустить экземпляр».
3. Выберите тип графического процессора и регион.
4. Выберите ключ SSH.
5. При необходимости прикрепите файловую систему
6. Запускаем и ждем 3-15 минут.

### Подключаемся через SSH

```bash
# Get instance IP from console
ssh ubuntu@<INSTANCE-IP>

# Or with specific key
ssh -i ~/.ssh/lambda_key ubuntu@<INSTANCE-IP>
```

## экземпляров графического процессора

### Доступные графические процессоры

| графический процессор | видеопамять | Цена/графический процессор/час | Лучшее для |
|-----|------|--------------|----------|
| Б200 SXM6 | 180 ГБ | $4,99 | Крупнейшие модели, самое быстрое обучение |
| H100 SXM | 80 ГБ | $2,99-3,29 | Обучение большой модели |
| H100 PCIe | 80 ГБ | 2,49 доллара США | Экономичный H100 |
| ГХ200 | 96 ГБ | 1,49 доллара США | Большие модели с одним графическим процессором |
| А100 80 ГБ | 80 ГБ | 1,79 доллара США | Производственное обучение |
| А100 40 ГБ | 40 ГБ | 1,29 доллара США | Стандартное обучение |
| А10 | 24 ГБ | $0,75 | Вывод, точная настройка |
| А6000 | 48 ГБ | 0,80 доллара США | Хорошее соотношение видеопамяти и цены |
| В100 | 16 ГБ | $0,55 | Бюджетное обучение |

### Конфигурации экземпляра

```
8x GPU: Best for distributed training (DDP, FSDP)
4x GPU: Large models, multi-GPU training
2x GPU: Medium workloads
1x GPU: Fine-tuning, inference, development
```

### Время запуска

- Один графический процессор: 3–5 минут
- Мульти-GPU: 10-15 минут

## Лямбда-стек

Все экземпляры поставляются с предустановленным Lambda Stack:

```bash
# Included software
- Ubuntu 22.04 LTS
- NVIDIA drivers (latest)
- CUDA 12.x
- cuDNN 8.x
- NCCL (for multi-GPU)
- PyTorch (latest)
- TensorFlow (latest)
- JAX
- JupyterLab
```

### Проверьте установку

```bash
# Check GPU
nvidia-smi

# Check PyTorch
python -c "import torch; print(torch.cuda.is_available())"

# Check CUDA version
nvcc --version
```

## API Python

### Установка

```bash
pip install lambda-cloud-client
```

### Аутентификация

```python
import os
import lambda_cloud_client

# Configure with API key
configuration = lambda_cloud_client.Configuration(
    host="https://cloud.lambdalabs.com/api/v1",
    access_token=os.environ["LAMBDA_API_KEY"]
)
```

### Получение списка доступных экземпляров

```python
with lambda_cloud_client.ApiClient(configuration) as api_client:
    api = lambda_cloud_client.DefaultApi(api_client)

    # Get available instance types
    types = api.instance_types()
    for name, info in types.data.items():
        print(f"{name}: {info.instance_type.description}")
```

### Запуск экземпляра

```python
from lambda_cloud_client.models import LaunchInstanceRequest

request = LaunchInstanceRequest(
    region_name="us-west-1",
    instance_type_name="gpu_1x_h100_sxm5",
    ssh_key_names=["my-ssh-key"],
    file_system_names=["my-filesystem"],  # Optional
    name="training-job"
)

response = api.launch_instance(request)
instance_id = response.data.instance_ids[0]
print(f"Launched: {instance_id}")
```

### Получение списка запущенных экземпляров

```python
instances = api.list_instances()
for instance in instances.data:
    print(f"{instance.name}: {instance.ip} ({instance.status})")
```

### Завершить экземпляр

```python
from lambda_cloud_client.models import TerminateInstanceRequest

request = TerminateInstanceRequest(
    instance_ids=[instance_id]
)
api.terminate_instance(request)
```

### Управление ключами SSH

```python
from lambda_cloud_client.models import AddSshKeyRequest

# Add SSH key
request = AddSshKeyRequest(
    name="my-key",
    public_key="ssh-rsa AAAA..."
)
api.add_ssh_key(request)

# List keys
keys = api.list_ssh_keys()

# Delete key
api.delete_ssh_key(key_id)
```

## CLI с завитком

### Список типов экземпляров

```bash
curl -u $LAMBDA_API_KEY: \
  https://cloud.lambdalabs.com/api/v1/instance-types | jq
```

### Запуск экземпляра

```bash
curl -u $LAMBDA_API_KEY: \
  -X POST https://cloud.lambdalabs.com/api/v1/instance-operations/launch \
  -H "Content-Type: application/json" \
  -d '{
    "region_name": "us-west-1",
    "instance_type_name": "gpu_1x_h100_sxm5",
    "ssh_key_names": ["my-key"]
  }' | jq
```

### Завершить экземпляр

```bash
curl -u $LAMBDA_API_KEY: \
  -X POST https://cloud.lambdalabs.com/api/v1/instance-operations/terminate \
  -H "Content-Type: application/json" \
  -d '{"instance_ids": ["<INSTANCE-ID>"]}' | jq
```

## Постоянное хранилище

### Файловые системы

Файловые системы сохраняют данные при перезапуске экземпляра:

```bash
# Mount location
/lambda/nfs/<FILESYSTEM_NAME>

# Example: save checkpoints
python train.py --checkpoint-dir /lambda/nfs/my-storage/checkpoints
```

### Создать файловую систему

1. Перейдите в раздел «Хранилище» в консоли Lambda.
2. Нажмите «Создать файловую систему».
3. Выберите регион (должен совпадать с регионом экземпляра)
4. Назовите и создайте

### Прикрепить к экземпляру

Файловые системы должны быть подключены во время запуска экземпляра:
- Через консоль: выберите файловую систему при запуске.
– Через API: включите `file_system_names` в запрос на запуск.

### Лучшие практики

<!-- ascii-guard-ignore -->
```bash
# Store on filesystem (persists)
/lambda/nfs/storage/
  ├── datasets/
  ├── checkpoints/
  ├── models/
  └── outputs/

# Local SSD (faster, ephemeral)
~/ (instance home)
  └── working/  # Temporary files
```
<!-- ascii-guard-ignore-end -->

## Конфигурация SSH

### Добавляем SSH-ключ

```bash
# Generate key locally
ssh-keygen -t ed25519 -f ~/.ssh/lambda_key

# Add public key to Lambda console
# Or via API
```

### Несколько ключей

```bash
# On instance, add more keys
echo 'ssh-rsa AAAA...' >> ~/.ssh/authorized_keys
```

### Импорт из GitHub

```bash
# On instance
ssh-import-id gh:username
```

### SSH-туннелирование

```bash
# Forward Jupyter
ssh -L 8888:localhost:8888 ubuntu@<IP>

# Forward TensorBoard
ssh -L 6006:localhost:6006 ubuntu@<IP>

# Multiple ports
ssh -L 8888:localhost:8888 -L 6006:localhost:6006 ubuntu@<IP>
```

## ЮпитерЛаб

### Запуск из консоли

1. Перейдите на страницу экземпляров.
2. Нажмите «Запустить» в столбце Cloud IDE.
3. JupyterLab открывается в браузере.

### Ручной доступ

```bash
# On instance
jupyter lab --ip=0.0.0.0 --port=8888

# From local machine with tunnel
ssh -L 8888:localhost:8888 ubuntu@<IP>
# Open http://localhost:8888
```

## Рабочие процессы обучения

### Обучение на одном графическом процессоре

```bash
# SSH to instance
ssh ubuntu@<IP>

# Clone repo
git clone https://github.com/user/project
cd project

# Install dependencies
pip install -r requirements.txt

# Train
python train.py --epochs 100 --checkpoint-dir /lambda/nfs/storage/checkpoints
```

### Обучение нескольких графических процессоров (один узел)

```python
# train_ddp.py
import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP

def main():
    dist.init_process_group("nccl")
    rank = dist.get_rank()
    device = rank % torch.cuda.device_count()

    model = MyModel().to(device)
    model = DDP(model, device_ids=[device])

    # Training loop...

if __name__ == "__main__":
    main()
```

```bash
# Launch with torchrun (8 GPUs)
torchrun --nproc_per_node=8 train_ddp.py
```

### Контрольная точка файловой системы

```python
import os

checkpoint_dir = "/lambda/nfs/my-storage/checkpoints"
os.makedirs(checkpoint_dir, exist_ok=True)

# Save checkpoint
torch.save({
    'epoch': epoch,
    'model_state_dict': model.state_dict(),
    'optimizer_state_dict': optimizer.state_dict(),
    'loss': loss,
}, f"{checkpoint_dir}/checkpoint_{epoch}.pt")
```

## Кластеры в один клик

### Обзор

Высокопроизводительные кластеры Slurm с:
- 16-512 графических процессоров NVIDIA H100 или B200
- NVIDIA Quantum-2 400 Гбит/с InfiniBand
- GPUDirect RDMA со скоростью 3200 Гбит/с
- Предустановленный распределенный стек машинного обучения.

### Включенное программное обеспечение

- Ubuntu 22.04 LTS + лямбда-стек
- NCCL, Открытый MPI
- PyTorch с DDP и FSDP
- ТензорФлоу
- Водители ОФЭД

### Хранилище

- 24 ТБ NVMe на каждый вычислительный узел (временно)
- Файловые системы Lambda для постоянных данных.

### Многоузловое обучение

```bash
# On Slurm cluster
srun --nodes=4 --ntasks-per-node=8 --gpus-per-node=8 \
  torchrun --nnodes=4 --nproc_per_node=8 \
  --rdzv_backend=c10d --rdzv_endpoint=$MASTER_ADDR:29500 \
  train.py
```

## Сеть

### Пропускная способность

- Между экземплярами (один и тот же регион): до 200 Гбит/с.
- Исходящий Интернет: максимум 20 Гбит/с.

### Брандмауэр

- По умолчанию: открыт только порт 22 (SSH).
- Настройте дополнительные порты в консоли Lambda.
- ICMP-трафик разрешен по умолчанию

### Частные IP-адреса

```bash
# Find private IP
ip addr show | grep 'inet '
```

## Общие рабочие процессы

### Рабочий процесс 1. Тонкая настройка LLM

```bash
# 1. Launch 8x H100 instance with filesystem

# 2. SSH and setup
ssh ubuntu@<IP>
pip install transformers accelerate peft

# 3. Download model to filesystem
python -c "
from transformers import AutoModelForCausalLM
model = AutoModelForCausalLM.from_pretrained('meta-llama/Llama-2-7b-hf')
model.save_pretrained('/lambda/nfs/storage/models/llama-2-7b')
"

# 4. Fine-tune with checkpoints on filesystem
accelerate launch --num_processes 8 train.py \
  --model_path /lambda/nfs/storage/models/llama-2-7b \
  --output_dir /lambda/nfs/storage/outputs \
  --checkpoint_dir /lambda/nfs/storage/checkpoints
```

### Рабочий процесс 2: Пакетный вывод

```bash
# 1. Launch A10 instance (cost-effective for inference)

# 2. Run inference
python inference.py \
  --model /lambda/nfs/storage/models/fine-tuned \
  --input /lambda/nfs/storage/data/inputs.jsonl \
  --output /lambda/nfs/storage/data/outputs.jsonl
```

## Оптимизация затрат

### Выберите правильный графический процессор

| Задача | Рекомендуемый графический процессор |
|------|-----------------|
| Тонкая настройка LLM (7B) | А100 40 ГБ |
| LLM тонкая настройка (70B) | 8x H100 |
| Вывод | А10, А6000 |
| Развитие | В100, А10 |
| Максимальная производительность | Б200 |

### Сократите затраты

1. **Используйте файловые системы**: избегайте повторной загрузки данных.
2. **Часто проверяйте точку**: возобновляйте прерванную тренировку.
3. **Правильный размер**: не переусердствуйте с графическими процессорами.
4. **Завершение простоя**: нет автоматической остановки, завершение вручную.

### Мониторинг использования

- Панель мониторинга показывает использование графического процессора в реальном времени.
- API для программного мониторинга

## Распространенные проблемы

| Выпуск | Решение |
|-------|----------|
| Экземпляр не запускается | Проверьте доступность региона, попробуйте другой графический процессор |
| SSH-соединение отклонено | Подождите, например, инициализации (3–15 минут) |
| Данные потеряны после завершения | Использовать постоянные файловые системы |
| Медленная передача данных | Использовать файловую систему в том же регионе |
| Графический процессор не обнаружен | Перезагрузите экземпляр, проверьте драйверы |

## Ссылки

- **[Расширенное использование](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/lambda-labs/references/advanced-usage.md)** - Многоузловое обучение, автоматизация API
- **[Устранение неполадок](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/lambda-labs/references/troubleshooting.md)** – Распространенные проблемы и решения

## Ресурсы

- **Документация**: https://docs.lambda.ai.
- **Консоль**: https://cloud.lambda.ai
- **Цены**: https://lambda.ai/instances
- **Поддержка**: https://support.lambdalabs.com.
- **Блог**: https://lambda.ai/blog