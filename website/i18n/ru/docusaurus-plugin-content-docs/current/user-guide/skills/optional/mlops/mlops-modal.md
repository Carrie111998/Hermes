---
title: Модальное — бессерверное облако графического процессора для заданий машинного
  обучения и API-интерфейсов моделей.
sidebar_label: Modal
description: Бессерверное облако графических процессоров для заданий машинного обучения
  и API моделей
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Модальное окно

Бессерверное облако графических процессоров для заданий машинного обучения и API-интерфейсов моделей.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/mlops/modal` |
| Путь | `optional-skills/mlops/modal` |
| Версия | `1.0.1` |
| Автор | Исследование оркестра |
| Лицензия | Массачусетский технологический институт |
| Зависимости | `modal>=1.0` |
| Платформы | Linux, MacOS, Windows |
| Теги | `Infrastructure`, `Serverless`, `GPU`, `Cloud`, `Deployment`, `Modal` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Модальный бессерверный графический процессор

Руководство по запуску рабочих нагрузок машинного обучения на бессерверной облачной платформе графического процессора Modal.

## Когда использовать модальное окно

**Используйте модальное окно, когда:**
- Выполнение рабочих нагрузок машинного обучения с интенсивным использованием графических процессоров без управления инфраструктурой.
- Развертывание моделей машинного обучения в виде API автоматического масштабирования.
- Выполнение заданий пакетной обработки (обучение, вывод, обработка данных)
- Нужны цены на графические процессоры с посекундной оплатой без затрат на простой
- Быстрое создание прототипов приложений ML.
- Запуск запланированных заданий (рабочие нагрузки, подобные cron)

**Основные особенности:**
- **Бессерверные графические процессоры**: T4, L4, A10G, L40S, A100, H100, H200, B200 по требованию.
- **Python-native**: определение инфраструктуры в коде Python, без YAML.
- **Автомасштабирование**: масштабирование до нуля, мгновенное масштабирование до 100+ графических процессоров.
- **Холодный запуск за доли секунды**: инфраструктура на основе Rust для быстрого запуска контейнеров.
- **Кэширование контейнера**: слои изображения кэшируются для быстрой итерации.
- **Веб-конечные точки**: развертывайте функции как REST API с обновлениями без простоев.

**Вместо этого используйте альтернативы:**
- **RunPod**: для долго работающих модулей с постоянным состоянием.
- **Lambda Labs**: для зарезервированных экземпляров графического процессора.
- **SkyPilot**: для оркестровки нескольких облаков и оптимизации затрат.
- **Kubernetes**: для сложных мультисервисных архитектур.

## Быстрый старт

### Установка

```bash
pip install modal
modal setup  # Opens browser for authentication
```

### Привет, мир с графическим процессором

```python
import modal

app = modal.App("hello-gpu")

@app.function(gpu="T4")
def gpu_info():
    import subprocess
    return subprocess.run(["nvidia-smi"], capture_output=True, text=True).stdout

@app.local_entrypoint()
def main():
    print(gpu_info.remote())
```

Запуск: `modal run hello_gpu.py`

### Базовая конечная точка вывода

```python
import modal

app = modal.App("text-generation")
image = modal.Image.debian_slim().pip_install("transformers", "torch", "accelerate")

@app.cls(gpu="A10G", image=image)
class TextGenerator:
    @modal.enter()
    def load_model(self):
        from transformers import pipeline
        self.pipe = pipeline("text-generation", model="gpt2", device=0)

    @modal.method()
    def generate(self, prompt: str) -> str:
        return self.pipe(prompt, max_length=100)[0]["generated_text"]

@app.local_entrypoint()
def main():
    print(TextGenerator().generate.remote("Hello, world"))
```

## Основные понятия

### Ключевые компоненты

| Компонент | Цель |
|-----------|---------|
| `App` | Контейнер для функций и ресурсов |
| `Function` | Бессерверная функция с вычислительными характеристиками |
| `Cls` | Функции на основе классов с перехватчиками жизненного цикла |
| `Image` | Определение образа контейнера |
| `Volume` | Постоянное хранилище моделей/данных |
| `Secret` | Безопасное хранение учетных данных |

### Режимы выполнения

| Команда | Описание |
|---------|-------------|
| `modal run script.py` | Выполнить и выйти |
| `modal serve script.py` | Разработка с живой перезагрузкой |
| `modal deploy script.py` | Постоянное развертывание в облаке |

## Конфигурация графического процессора

### Доступные графические процессоры

| графический процессор | видеопамять | Лучшее для |
|-----|------|----------|
| `T4` | 16 ГБ | Бюджетный вывод, небольшие модели |
| `L4` | 24 ГБ | Вывод, арка Ады Лавлейс |
| `A10G` | 24 ГБ | Обучение/вывод: в 3,3 раза быстрее, чем T4 |
| `L40S` | 48 ГБ | Рекомендуется для вывода (лучшая цена/производительность) |
| `A100-40GB` | 40 ГБ | Обучение большой модели |
| `A100-80GB` | 80 ГБ | Очень большие модели |
| `H100` | 80 ГБ | Самый быстрый двигатель FP8 + трансформатор |
| `H200` | 141 ГБ | Автоматическое обновление с H100, пропускная способность 4,8 ТБ/с |
| `B200` | Последние | Архитектура Блэквелла |

### Шаблоны спецификаций графического процессора

```python
# Single GPU
@app.function(gpu="A100")

# Specific memory variant
@app.function(gpu="A100-80GB")

# Multiple GPUs (up to 8)
@app.function(gpu="H100:4")

# GPU with fallbacks
@app.function(gpu=["H100", "A100", "L40S"])

# Any available GPU
@app.function(gpu="any")
```

## Образы контейнеров

```python
# Basic image with pip
image = modal.Image.debian_slim(python_version="3.11").pip_install(
    "torch==2.1.0", "transformers==4.36.0", "accelerate"
)

# From CUDA base
image = modal.Image.from_registry(
    "nvidia/cuda:12.1.0-cudnn8-devel-ubuntu22.04",
    add_python="3.11"
).pip_install("torch", "transformers")

# With system packages
image = modal.Image.debian_slim().apt_install("git", "ffmpeg").pip_install("whisper")
```

## Постоянное хранилище

```python
volume = modal.Volume.from_name("model-cache", create_if_missing=True)

@app.function(gpu="A10G", volumes={"/models": volume})
def load_model():
    import os
    model_path = "/models/llama-7b"
    if not os.path.exists(model_path):
        model = download_model()
        model.save_pretrained(model_path)
        volume.commit()  # Persist changes
    return load_from_path(model_path)
```

## Веб-конечные точки

### Декоратор конечной точки FastAPI

```python
@app.function()
@modal.fastapi_endpoint(method="POST")
def predict(text: str) -> dict:
    return {"result": model.predict(text)}
```

### Полное приложение ASGI

```python
from fastapi import FastAPI
web_app = FastAPI()

@web_app.post("/predict")
async def predict(text: str):
    return {"result": await model.predict.remote.aio(text)}

@app.function()
@modal.asgi_app()
def fastapi_app():
    return web_app
```

### Типы веб-конечных точек

| Декоратор | Вариант использования |
|-----------|----------|
| `@modal.fastapi_endpoint()` | Простая функция → API |
| `@modal.asgi_app()` | Полные приложения FastAPI/Starlette |
| `@modal.wsgi_app()` | Приложения Django/Flask |
| `@modal.web_server(port)` | Произвольные HTTP-серверы |

## Динамическая пакетная обработка

```python
@app.function()
@modal.batched(max_batch_size=32, wait_ms=100)
async def batch_predict(inputs: list[str]) -> list[dict]:
    # Inputs automatically batched
    return model.batch_predict(inputs)
```
## Управление секретами

```bash
# Create secret
modal secret create huggingface HF_TOKEN=hf_xxx
```

```python
@app.function(secrets=[modal.Secret.from_name("huggingface")])
def download_model():
    import os
    token = os.environ["HF_TOKEN"]
```

## Планирование

```python
@app.function(schedule=modal.Cron("0 0 * * *"))  # Daily midnight
def daily_job():
    pass

@app.function(schedule=modal.Period(hours=1))
def hourly_job():
    pass
```

## Оптимизация производительности

### Устранение холодного запуска

```python
# Modal 1.0 autoscaler params: scaledown_window (was container_idle_timeout).
# Input concurrency moved to the @modal.concurrent decorator.
@app.function(scaledown_window=300)  # Keep warm 5 min
@modal.concurrent(max_inputs=10)     # Handle concurrent requests per container
def inference():
    pass
```

### Рекомендации по загрузке моделей

```python
@app.cls(gpu="A100")
class Model:
    @modal.enter()  # Run once at container start
    def load(self):
        self.model = load_model()  # Load during warm-up

    @modal.method()
    def predict(self, x):
        return self.model(x)
```

## Параллельная обработка

```python
@app.function()
def process_item(item):
    return expensive_computation(item)

@app.function()
def run_parallel():
    items = list(range(1000))
    # Fan out to parallel containers
    results = list(process_item.map(items))
    return results
```

## Общая конфигурация

```python
@app.function(
    gpu="A100",
    memory=32768,              # 32GB RAM
    cpu=4,                     # 4 CPU cores
    timeout=3600,              # 1 hour max
    scaledown_window=120,      # Keep warm 2 min (was container_idle_timeout)
    retries=3,                 # Retry on failure
    max_containers=10,         # Max concurrent containers (was concurrency_limit)
    min_containers=1,          # Keep N containers warm (was keep_warm)
)
def my_function():
    pass
```

> **Переименовывание модуля автомасштабирования Modal 1.0** (см. [руководство по миграции](https://modal.com/docs/guide/modal-1-0-migration)):
> - `container_idle_timeout` → `scaledown_window`
> - `concurrency_limit` → `max_containers`
> - `keep_warm` → `min_containers`
> - `allow_concurrent_inputs=N` → декоратор `@modal.concurrent(max_inputs=N)`

## Отладка

```python
# Test locally
if __name__ == "__main__":
    result = my_function.local()

# View logs
# modal app logs my-app
```

## Распространенные проблемы

| Выпуск | Решение |
|-------|----------|
| Задержка холодного старта | Увеличьте `scaledown_window`, используйте `@modal.enter()` |
| ГПУ ООМ | Используйте более крупный графический процессор (`A100-80GB`), включите контрольную точку градиента |
| Сборка образа не удалась | Версии зависимостей выводов, проверьте совместимость CUDA |
| Ошибки тайм-аута | Увеличьте `timeout`, добавьте контрольную точку |

## Ссылки

- **[Расширенное использование](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/modal/references/advanced-usage.md)** - Multi-GPU, распределенное обучение, оптимизация затрат
- **[Устранение неполадок](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/modal/references/troubleshooting.md)** – Распространенные проблемы и решения

## Ресурсы

- **Документация**: https://modal.com/docs.
- **Примеры**: https://github.com/modal-labs/modal-examples.
- **Цены**: https://modal.com/pricing.
- **Discord**: https://discord.gg/modal