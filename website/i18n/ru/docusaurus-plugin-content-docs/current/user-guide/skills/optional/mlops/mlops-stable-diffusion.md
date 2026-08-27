---
title: Stable Diffusion — генерация текста в изображение, зарисовка и img2img.
sidebar_label: Stable Diffusion
description: Генерация текста в изображение, рисование и img2img
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Стабильная диффузия

Генерация текста в изображение, рисование и img2img.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/mlops/stable-diffusion` |
| Путь | `optional-skills/mlops/stable-diffusion` |
| Версия | `1.0.0` |
| Автор | Исследование оркестра |
| Лицензия | Массачусетский технологический институт |
| Зависимости | `diffusers>=0.30.0`, `transformers>=4.41.0`, `accelerate>=0.31.0`, `torch>=2.0.0` |
| Платформы | Linux, MacOS, Windows |
| Теги | `Image Generation`, `Stable Diffusion`, `Diffusers`, `Text-to-Image`, `Multimodal`, `Computer Vision` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Генерация стабильного диффузионного изображения

Руководство по созданию изображений с помощью Stable Diffusion с использованием библиотеки HuggingFace Diffusers.

## Когда использовать стабильную диффузию

**Используйте стабильную диффузию, когда:**
- Генерация изображений из текстовых описаний
- Выполнение перевода изображения в изображение (перенос стиля, улучшение)
- Inpainting (заполнение замаскированных областей)
- Перерисовка (расширение изображений за пределы границ)
- Создание вариаций существующих изображений.
- Создание пользовательских рабочих процессов создания изображений.

**Основные особенности:**
- **Преобразование текста в изображение**: создание изображений на основе подсказок на естественном языке.
- **Изображение в изображение**: преобразуйте существующие изображения с помощью текстовых указаний.
- **Inpainting**: заполнение замаскированных областей контекстно-зависимым содержимым.
- **ControlNet**: добавление пространственной обусловленности (края, позы, глубина).
- **Поддержка LoRA**: эффективная точная настройка и адаптация стиля.
- **Несколько моделей**: SD 1.5, SDXL, SD 3.0, поддержка Flux.

**Вместо этого используйте альтернативы:**
- **DALL-E 3**: для генерации на основе API без графического процессора.
- **Midjourney**: для художественных, стилизованных результатов.
- **Imagen**: для интеграции с Google Cloud.
- **Leonardo.ai**: для творческих рабочих процессов через Интернет.

## Быстрый старт

### Установка

```bash
pip install diffusers transformers accelerate torch
pip install xformers  # Optional: memory-efficient attention
```

### Базовое преобразование текста в изображение

```python
from diffusers import DiffusionPipeline
import torch

# Load pipeline (auto-detects model type)
pipe = DiffusionPipeline.from_pretrained(
    "stable-diffusion-v1-5/stable-diffusion-v1-5",
    torch_dtype=torch.float16
)
pipe.to("cuda")

# Generate image
image = pipe(
    "A serene mountain landscape at sunset, highly detailed",
    num_inference_steps=50,
    guidance_scale=7.5
).images[0]

image.save("output.png")
```

### Использование SDXL (более высокое качество)

```python
from diffusers import AutoPipelineForText2Image
import torch

pipe = AutoPipelineForText2Image.from_pretrained(
    "stabilityai/stable-diffusion-xl-base-1.0",
    torch_dtype=torch.float16,
    variant="fp16"
)
pipe.to("cuda")

# Enable memory optimization
pipe.enable_model_cpu_offload()

image = pipe(
    prompt="A futuristic city with flying cars, cinematic lighting",
    height=1024,
    width=1024,
    num_inference_steps=30
).images[0]
```

## Обзор архитектуры

### Трехколонная конструкция

Диффузоры построены на трех основных компонентах:

<!-- ascii-guard-ignore -->
```
Pipeline (orchestration)
├── Model (neural networks)
│   ├── UNet / Transformer (noise prediction)
│   ├── VAE (latent encoding/decoding)
│   └── Text Encoder (CLIP/T5)
└── Scheduler (denoising algorithm)
```
<!-- ascii-guard-ignore-end -->

### Поток вывода конвейера

```
Text Prompt → Text Encoder → Text Embeddings
                                    ↓
Random Noise → [Denoising Loop] ← Scheduler
                      ↓
               Predicted Noise
                      ↓
              VAE Decoder → Final Image
```

## Основные понятия

### Трубопроводы

Конвейеры организуют полный рабочий процесс:

| Трубопровод | Цель |
|----------|---------|
| `StableDiffusionPipeline` | Преобразование текста в изображение (SD 1.x/2.x) |
| `StableDiffusionXLPipeline` | Преобразование текста в изображение (SDXL) |
| `StableDiffusion3Pipeline` | Преобразование текста в изображение (SD 3.0) |
| `FluxPipeline` | Преобразование текста в изображение (модели Flux) |
| `StableDiffusionImg2ImgPipeline` | Изображение к изображению |
| `StableDiffusionInpaintPipeline` | Живопись |

### Планировщики

Планировщики управляют процессом шумоподавления:

| Планировщик | Шаги | Качество | Вариант использования |
|-----------|-------|---------|----------|
| `EulerDiscreteScheduler` | 20-50 | Хорошо | Выбор по умолчанию |
| `EulerAncestralDiscreteScheduler` | 20-50 | Хорошо | Больше вариаций |
| `DPMSolverMultistepScheduler` | 15-25 | Отлично | Быстро, качественно |
| `DDIMScheduler` | 50-100 | Хорошо | Детерминированный |
| `LCMScheduler` | 4-8 | Хорошо | Очень быстро |
| `UniPCMultistepScheduler` | 15-25 | Отлично | Быстрая сходимость |

### Замена планировщиков

```python
from diffusers import DPMSolverMultistepScheduler

# Swap for faster generation
pipe.scheduler = DPMSolverMultistepScheduler.from_config(
    pipe.scheduler.config
)

# Now generate with fewer steps
image = pipe(prompt, num_inference_steps=20).images[0]
```

## Параметры генерации

### Ключевые параметры

| Параметр | По умолчанию | Описание |
|-----------|---------|-------------|
| `prompt` | Требуется | Текстовое описание желаемого изображения |
| `negative_prompt` | Нет | Чего следует избегать в изображении |
| `num_inference_steps` | 50 | Шаги шумоподавления (больше = лучшее качество) |
| `guidance_scale` | 7,5 | Быстрое соблюдение режима лечения (типично 7–12) |
| `height`, `width` | 512/1024 | Выходные размеры (кратные 8) |
| `generator` | Нет | Генератор факела для воспроизводимости |
| `num_images_per_prompt` | 1 | Размер партии |

### Воспроизводимое поколение

```python
import torch

generator = torch.Generator(device="cuda").manual_seed(42)

image = pipe(
    prompt="A cat wearing a top hat",
    generator=generator,
    num_inference_steps=50
).images[0]
```

### Отрицательные подсказки

```python
image = pipe(
    prompt="Professional photo of a dog in a garden",
    negative_prompt="blurry, low quality, distorted, ugly, bad anatomy",
    guidance_scale=7.5
).images[0]
```

## Изображение к изображению

Преобразуйте существующие изображения с помощью текстовых указаний:

```python
from diffusers import AutoPipelineForImage2Image
from PIL import Image

pipe = AutoPipelineForImage2Image.from_pretrained(
    "stable-diffusion-v1-5/stable-diffusion-v1-5",
    torch_dtype=torch.float16
).to("cuda")

init_image = Image.open("input.jpg").resize((512, 512))

image = pipe(
    prompt="A watercolor painting of the scene",
    image=init_image,
    strength=0.75,  # How much to transform (0-1)
    num_inference_steps=50
).images[0]
```

## Inpainting

Заполните замаскированные области:

```python
from diffusers import AutoPipelineForInpainting
from PIL import Image

pipe = AutoPipelineForInpainting.from_pretrained(
    "runwayml/stable-diffusion-inpainting",
    torch_dtype=torch.float16
).to("cuda")

image = Image.open("photo.jpg")
mask = Image.open("mask.png")  # White = inpaint region

result = pipe(
    prompt="A red car parked on the street",
    image=image,
    mask_image=mask,
    num_inference_steps=50
).images[0]
```

## Контрольная сеть

Добавьте пространственную обусловленность для точного контроля:

```python
from diffusers import StableDiffusionControlNetPipeline, ControlNetModel
import torch

# Load ControlNet for edge conditioning
controlnet = ControlNetModel.from_pretrained(
    "lllyasviel/control_v11p_sd15_canny",
    torch_dtype=torch.float16
)

pipe = StableDiffusionControlNetPipeline.from_pretrained(
    "stable-diffusion-v1-5/stable-diffusion-v1-5",
    controlnet=controlnet,
    torch_dtype=torch.float16
).to("cuda")

# Use Canny edge image as control
control_image = get_canny_image(input_image)

image = pipe(
    prompt="A beautiful house in the style of Van Gogh",
    image=control_image,
    num_inference_steps=30
).images[0]
```

### Доступные сети управления

| КонтролНет | Тип ввода | Вариант использования |
|------------|------------|----------|
| `canny` | Краевые карты | Сохранить структуру |
| `openpose` | Поза скелетов | Человеческие позы |
| `depth` | Карты глубины | поколение с поддержкой 3D |
| `normal` | Нормальные карты | Детали поверхности |
| `mlsd` | Отрезки линий | Архитектурные линии |
| `scribble` | Грубые наброски | Эскиз в изображение |

## адаптеры LoRA

Загрузите настроенные адаптеры стилей:

```python
from diffusers import DiffusionPipeline

pipe = DiffusionPipeline.from_pretrained(
    "stable-diffusion-v1-5/stable-diffusion-v1-5",
    torch_dtype=torch.float16
).to("cuda")

# Load LoRA weights
pipe.load_lora_weights("path/to/lora", weight_name="style.safetensors")

# Generate with LoRA style
image = pipe("A portrait in the trained style").images[0]

# Adjust LoRA strength
pipe.fuse_lora(lora_scale=0.8)

# Unload LoRA
pipe.unload_lora_weights()
```

### Несколько LoRA

```python
# Load multiple LoRAs
pipe.load_lora_weights("lora1", adapter_name="style")
pipe.load_lora_weights("lora2", adapter_name="character")

# Set weights for each
pipe.set_adapters(["style", "character"], adapter_weights=[0.7, 0.5])

image = pipe("A portrait").images[0]
```

## Оптимизация памяти

### Включить разгрузку процессора

```python
# Model CPU offload - moves models to CPU when not in use
pipe.enable_model_cpu_offload()

# Sequential CPU offload - more aggressive, slower
pipe.enable_sequential_cpu_offload()
```

### Нарезка внимания

```python
# Reduce memory by computing attention in chunks
pipe.enable_attention_slicing()

# Or specific chunk size
pipe.enable_attention_slicing("max")
```

### xFormers эффективное внимание к памяти

```python
# Requires xformers package
pipe.enable_xformers_memory_efficient_attention()
```

### Нарезка VAE для больших изображений

```python
# Decode latents in tiles for large images
pipe.enable_vae_slicing()
pipe.enable_vae_tiling()
```

## Варианты моделей

### Загрузка различной точности

```python
# FP16 (recommended for GPU)
pipe = DiffusionPipeline.from_pretrained(
    "model-id",
    torch_dtype=torch.float16,
    variant="fp16"
)

# BF16 (better precision, requires Ampere+ GPU)
pipe = DiffusionPipeline.from_pretrained(
    "model-id",
    torch_dtype=torch.bfloat16
)
```

### Загрузка определенных компонентов

```python
from diffusers import UNet2DConditionModel, AutoencoderKL

# Load custom VAE
vae = AutoencoderKL.from_pretrained("stabilityai/sd-vae-ft-mse")

# Use with pipeline
pipe = DiffusionPipeline.from_pretrained(
    "stable-diffusion-v1-5/stable-diffusion-v1-5",
    vae=vae,
    torch_dtype=torch.float16
)
```

## Пакетная генерация

Эффективно создавайте несколько изображений:

```python
# Multiple prompts
prompts = [
    "A cat playing piano",
    "A dog reading a book",
    "A bird painting a picture"
]

images = pipe(prompts, num_inference_steps=30).images

# Multiple images per prompt
images = pipe(
    "A beautiful sunset",
    num_images_per_prompt=4,
    num_inference_steps=30
).images
```

## Общие рабочие процессы

### Рабочий процесс 1: Генерация высокого качества

```python
from diffusers import StableDiffusionXLPipeline, DPMSolverMultistepScheduler
import torch

# 1. Load SDXL with optimizations
pipe = StableDiffusionXLPipeline.from_pretrained(
    "stabilityai/stable-diffusion-xl-base-1.0",
    torch_dtype=torch.float16,
    variant="fp16"
)
pipe.to("cuda")
pipe.scheduler = DPMSolverMultistepScheduler.from_config(pipe.scheduler.config)
pipe.enable_model_cpu_offload()

# 2. Generate with quality settings
image = pipe(
    prompt="A majestic lion in the savanna, golden hour lighting, 8k, detailed fur",
    negative_prompt="blurry, low quality, cartoon, anime, sketch",
    num_inference_steps=30,
    guidance_scale=7.5,
    height=1024,
    width=1024
).images[0]
```

### Рабочий процесс 2: Быстрое прототипирование

```python
from diffusers import AutoPipelineForText2Image, LCMScheduler
import torch

# Use LCM for 4-8 step generation
pipe = AutoPipelineForText2Image.from_pretrained(
    "stabilityai/stable-diffusion-xl-base-1.0",
    torch_dtype=torch.float16
).to("cuda")

# Load LCM LoRA for fast generation
pipe.load_lora_weights("latent-consistency/lcm-lora-sdxl")
pipe.scheduler = LCMScheduler.from_config(pipe.scheduler.config)
pipe.fuse_lora()

# Generate in ~1 second
image = pipe(
    "A beautiful landscape",
    num_inference_steps=4,
    guidance_scale=1.0
).images[0]
```

## Распространенные проблемы

**CUDA не хватает памяти:**
```python
# Enable memory optimizations
pipe.enable_model_cpu_offload()
pipe.enable_attention_slicing()
pipe.enable_vae_slicing()

# Or use lower precision
pipe = DiffusionPipeline.from_pretrained(model_id, torch_dtype=torch.float16)
```

**Черно-шумовые изображения:**
```python
# Check VAE configuration
# Use safety checker bypass if needed
pipe.safety_checker = None

# Ensure proper dtype consistency
pipe = pipe.to(dtype=torch.float16)
```

**Медленная генерация:**
```python
# Use faster scheduler
from diffusers import DPMSolverMultistepScheduler
pipe.scheduler = DPMSolverMultistepScheduler.from_config(pipe.scheduler.config)

# Reduce steps
image = pipe(prompt, num_inference_steps=20).images[0]
```

## Ссылки

- **[Расширенное использование](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/stable-diffusion/references/advanced-usage.md)** - Пользовательские конвейеры, тонкая настройка, развертывание
- **[Устранение неполадок](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/stable-diffusion/references/troubleshooting.md)** – Распространенные проблемы и решения

## Ресурсы

- **Документация**: https://huggingface.co/docs/diffusers.
- **Репозиторий**: https://github.com/huggingface/diffusers.
- **Модельный центр**: https://huggingface.co/models?library=diffusers
- **Discord**: https://discord.gg/diffusers