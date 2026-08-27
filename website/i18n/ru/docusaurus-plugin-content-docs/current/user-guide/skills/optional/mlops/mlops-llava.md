---
title: 'Llava — Чат на языке Vision: VQA, субтитры, диалоги изображений'
sidebar_label: Llava
description: 'Чат на языке видения: VQA, субтитры, диалоги изображений'
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

#Ллава

Чат на языке видения: VQA, субтитры, диалоги изображений.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/mlops/llava` |
| Путь | `optional-skills/mlops/llava` |
| Версия | `1.0.0` |
| Автор | Исследование оркестра |
| Лицензия | Массачусетский технологический институт |
| Зависимости | `transformers`, `torch`, `pillow` |
| Платформы | Linux, MacOS, Windows |
| Теги | `LLaVA`, `Vision-Language`, `Multimodal`, `Visual Question Answering`, `Image Chat`, `CLIP`, `Vicuna`, `Conversational AI`, `Instruction Tuning`, `VQA` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# LLaVA — Помощник по большому языку и зрению

Модель языка видения с открытым исходным кодом для понимания диалоговых изображений.

## Когда использовать LLaVA

**Используйте, когда:**
- Создание чат-ботов на языке видения
- Визуальный ответ на вопрос (VQA)
- Описание изображения и подпись.
- Многооборотные диалоги изображений
- Визуальная инструкция после
- Понимание документа с изображениями

**Показатели**:
- **23 000+ звезд GitHub**
- Возможности уровня GPT-4V (целевые)
- Лицензия Апач 2.0
- Несколько размеров модели (параметры 7B-34B)

**Вместо этого используйте альтернативы**:
- **GPT-4V**: высочайшее качество, на основе API.
- **CLIP**: Простая классификация нулевых выстрелов.
- **BLIP-2**: лучше только для субтитров.
- **Фламинго**: исследования, а не открытый исходный код.

## Быстрый старт

### Установка

```bash
# Clone repository
git clone https://github.com/haotian-liu/LLaVA
cd LLaVA

# Install
pip install -e .
```

### Базовое использование

```python
from llava.model.builder import load_pretrained_model
from llava.mm_utils import get_model_name_from_path, process_images, tokenizer_image_token
from llava.constants import IMAGE_TOKEN_INDEX, DEFAULT_IMAGE_TOKEN
from llava.conversation import conv_templates
from PIL import Image
import torch

# Load model
model_path = "liuhaotian/llava-v1.5-7b"
tokenizer, model, image_processor, context_len = load_pretrained_model(
    model_path=model_path,
    model_base=None,
    model_name=get_model_name_from_path(model_path)
)

# Load image
image = Image.open("image.jpg")
image_tensor = process_images([image], image_processor, model.config)
image_tensor = image_tensor.to(model.device, dtype=torch.float16)

# Create conversation
conv = conv_templates["llava_v1"].copy()
conv.append_message(conv.roles[0], DEFAULT_IMAGE_TOKEN + "\nWhat is in this image?")
conv.append_message(conv.roles[1], None)
prompt = conv.get_prompt()

# Generate response
input_ids = tokenizer_image_token(prompt, tokenizer, IMAGE_TOKEN_INDEX, return_tensors='pt').unsqueeze(0).to(model.device)

with torch.inference_mode():
    output_ids = model.generate(
        input_ids,
        images=image_tensor,
        do_sample=True,
        temperature=0.2,
        max_new_tokens=512
    )

response = tokenizer.decode(output_ids[0], skip_special_tokens=True).strip()
print(response)
```

## Доступные модели

| Модель | Параметры | видеопамять | Качество |
|-------|------------|------|---------|
| LLaVA-v1.5-7B | 7Б | ~14 ГБ | Хорошо |
| LLaVA-v1.5-13B | 13Б | ~28 ГБ | Лучше |
| LLaVA-v1.6-34B | 34Б | ~70 ГБ | Лучшее |

```python
# Load different models
model_7b = "liuhaotian/llava-v1.5-7b"
model_13b = "liuhaotian/llava-v1.5-13b"
model_34b = "liuhaotian/llava-v1.6-34b"

# 4-bit quantization for lower VRAM
load_4bit = True  # Reduces VRAM by ~4×
```

## Использование CLI

```bash
# Single image query
python -m llava.serve.cli \
    --model-path liuhaotian/llava-v1.5-7b \
    --image-file image.jpg \
    --query "What is in this image?"

# Multi-turn conversation
python -m llava.serve.cli \
    --model-path liuhaotian/llava-v1.5-7b \
    --image-file image.jpg
# Then type questions interactively
```

## Веб-интерфейс (Градио)

```bash
# Launch Gradio interface
python -m llava.serve.gradio_web_server \
    --model-path liuhaotian/llava-v1.5-7b \
    --load-4bit  # Optional: reduce VRAM

# Access at http://localhost:7860
```

## Многоходовые разговоры

```python
# Initialize conversation
conv = conv_templates["llava_v1"].copy()

# Turn 1
conv.append_message(conv.roles[0], DEFAULT_IMAGE_TOKEN + "\nWhat is in this image?")
conv.append_message(conv.roles[1], None)
response1 = generate(conv, model, image)  # "A dog playing in a park"

# Turn 2
conv.messages[-1][1] = response1  # Add previous response
conv.append_message(conv.roles[0], "What breed is the dog?")
conv.append_message(conv.roles[1], None)
response2 = generate(conv, model, image)  # "Golden Retriever"

# Turn 3
conv.messages[-1][1] = response2
conv.append_message(conv.roles[0], "What time of day is it?")
conv.append_message(conv.roles[1], None)
response3 = generate(conv, model, image)
```

## Общие задачи

### Подпись к изображению

```python
question = "Describe this image in detail."
response = ask(model, image, question)
```

### Визуальный ответ на вопрос

```python
question = "How many people are in the image?"
response = ask(model, image, question)
```

### Обнаружение объектов (текстовое)

```python
question = "List all the objects you can see in this image."
response = ask(model, image, question)
```

### Понимание сцены

```python
question = "What is happening in this scene?"
response = ask(model, image, question)
```

### Понимание документа

```python
question = "What is the main topic of this document?"
response = ask(model, document_image, question)
```

## Обучение пользовательской модели

```bash
# Stage 1: Feature alignment (558K image-caption pairs)
bash scripts/v1_5/pretrain.sh

# Stage 2: Visual instruction tuning (150K instruction data)
bash scripts/v1_5/finetune.sh
```

## Квантование (уменьшение видеопамяти)

```python
# 4-bit quantization
tokenizer, model, image_processor, context_len = load_pretrained_model(
    model_path="liuhaotian/llava-v1.5-13b",
    model_base=None,
    model_name=get_model_name_from_path("liuhaotian/llava-v1.5-13b"),
    load_4bit=True  # Reduces VRAM ~4×
)

# 8-bit quantization
load_8bit=True  # Reduces VRAM ~2×
```

## Лучшие практики

1. **Начните с модели 7B** – хорошее качество, управляемая видеопамять.
2. **Использовать 4-битное квантование** — значительно сокращает объем видеопамяти.
3. **Требуется графический процессор** — вывод процессора очень медленный.
4. **Четкие подсказки** – на конкретные вопросы можно получить более точные ответы.
5. **Многоходовые разговоры** – поддержание контекста разговора.
6. **Температура 0,2–0,7** – баланс креативности и последовательности.
7. **max_new_tokens 512-1024** — для подробных ответов.
8. **Пакетная обработка** – последовательная обработка нескольких изображений.

## Производительность

| Модель | Видеопамять (FP16) | Видеопамять (4-битная) | Скорость (токены/с) |
|-------|-------------|--------------|------------------|
| 7Б | ~14 ГБ | ~4 ГБ | ~20 |
| 13Б | ~28 ГБ | ~8 ГБ | ~12 |
| 34Б | ~70 ГБ | ~18 ГБ | ~5 |

*На графическом процессоре A100*

## Тесты

LLaVA достигает конкурентных результатов по:
- **VQAv2**: 78,5%
- **GQA**: 62,0%
- **ММ-Вет**: 35,4%
- **MMBench**: 64,3%

## Ограничения

1. **Галлюцинации**. Могут описывать вещи, которых нет на изображении.
2. **Пространственное мышление**. Трудности с определением точного местоположения.
3. **Мелкий текст**. Трудно прочитать мелкий шрифт.
4. **Подсчет объектов** – для многих объектов неточный результат.
5. **Требования к видеопамяти** – нужен мощный графический процессор.
6. **Скорость вывода** – медленнее, чем CLIP.

## Интеграция с фреймворками

### Лангчейн

```python
from langchain.llms.base import LLM

class LLaVALLM(LLM):
    def _call(self, prompt, stop=None):
        # Custom LLaVA inference
        return response

llm = LLaVALLM()
```

### Приложение Градио

```python
import gradio as gr

def chat(image, text, history):
    response = ask_llava(model, image, text)
    return response

demo = gr.ChatInterface(
    chat,
    additional_inputs=[gr.Image(type="pil")],
    title="LLaVA Chat"
)
demo.launch()
```

## Ресурсы

- **GitHub**: https://github.com/haotian-liu/LLaVA ⭐ 23 000+
- **Бумага**: https://arxiv.org/abs/2304.08485.
- **Демо**: https://llava.hliu.cc
- **Модели**: https://huggingface.co/liuhaotian
- **Лицензия**: Apache 2.0.