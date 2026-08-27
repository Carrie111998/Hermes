---
title: Clip — Zero-shot image classification and image-text search
sidebar_label: Clip
description: Классификация изображений с нулевым кадром и поиск по изображению и тексту
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Клип

Классификация изображений с нулевым кадром и поиск по изображению и тексту.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/mlops/clip` |
| Путь | `optional-skills/mlops/clip` |
| Версия | `1.0.0` |
| Автор | Исследование оркестра |
| Лицензия | Массачусетский технологический институт |
| Зависимости | `transformers`, `torch`, `pillow` |
| Платформы | Linux, MacOS, Windows |
| Теги | `Multimodal`, `CLIP`, `Vision-Language`, `Zero-Shot`, `Image Classification`, `OpenAI`, `Image Search`, `Cross-Modal Retrieval`, `Content Moderation` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# CLIP — Предварительная тренировка по контрастному языку и изображению

Модель OpenAI, которая понимает изображения на естественном языке.

## Когда использовать CLIP

**Используйте, когда:**
- Классификация изображений с нулевым выстрелом (данные для обучения не требуются)
- Сходство/совпадение изображения и текста
- Семантический поиск изображений
- Модерация контента (обнаружение NSFW, насилия)
- Визуальный ответ на вопрос
- Кросс-модальный поиск (изображение→текст, текст→изображение)

**Показатели**:
- **25 300+ звезд GitHub**
- Обучено на 400 миллионах пар изображение-текст.
- Соответствует ResNet-50 на ImageNet (нулевой выстрел)
- Лицензия Массачусетского технологического института

**Вместо этого используйте альтернативы**:
- **BLIP-2**: улучшенные субтитры.
- **LLaVA**: чат на языке видения.
- **Сегментировать что угодно**: сегментация изображений.

## Быстрый старт

### Установка

```bash
pip install git+https://github.com/openai/CLIP.git
pip install torch torchvision ftfy regex tqdm
```

### Классификация нулевого выстрела

```python
import torch
import clip
from PIL import Image

# Load model
device = "cuda" if torch.cuda.is_available() else "cpu"
model, preprocess = clip.load("ViT-B/32", device=device)

# Load image
image = preprocess(Image.open("photo.jpg")).unsqueeze(0).to(device)

# Define possible labels
text = clip.tokenize(["a dog", "a cat", "a bird", "a car"]).to(device)

# Compute similarity
with torch.no_grad():
    image_features = model.encode_image(image)
    text_features = model.encode_text(text)

    # Cosine similarity
    logits_per_image, logits_per_text = model(image, text)
    probs = logits_per_image.softmax(dim=-1).cpu().numpy()

# Print results
labels = ["a dog", "a cat", "a bird", "a car"]
for label, prob in zip(labels, probs[0]):
    print(f"{label}: {prob:.2%}")
```

## Доступные модели

```python
# Models (sorted by size)
models = [
    "RN50",           # ResNet-50
    "RN101",          # ResNet-101
    "ViT-B/32",       # Vision Transformer (recommended)
    "ViT-B/16",       # Better quality, slower
    "ViT-L/14",       # Best quality, slowest
]

model, preprocess = clip.load("ViT-B/32")
```

| Модель | Параметры | Скорость | Качество |
|-------|------------|-------|---------|
| РН50 | 102М | Быстро | Хорошо |
| ВИТ-Б/32 | 151М | Средний | Лучше |
| ВиТ-Л/14 | 428М | Медленно | Лучшее |

## Сходство изображения и текста

```python
# Compute embeddings
image_features = model.encode_image(image)
text_features = model.encode_text(text)

# Normalize
image_features /= image_features.norm(dim=-1, keepdim=True)
text_features /= text_features.norm(dim=-1, keepdim=True)

# Cosine similarity
similarity = (image_features @ text_features.T).item()
print(f"Similarity: {similarity:.4f}")
```

## Семантический поиск изображений

```python
# Index images
image_paths = ["img1.jpg", "img2.jpg", "img3.jpg"]
image_embeddings = []

for img_path in image_paths:
    image = preprocess(Image.open(img_path)).unsqueeze(0).to(device)
    with torch.no_grad():
        embedding = model.encode_image(image)
        embedding /= embedding.norm(dim=-1, keepdim=True)
    image_embeddings.append(embedding)

image_embeddings = torch.cat(image_embeddings)

# Search with text query
query = "a sunset over the ocean"
text_input = clip.tokenize([query]).to(device)
with torch.no_grad():
    text_embedding = model.encode_text(text_input)
    text_embedding /= text_embedding.norm(dim=-1, keepdim=True)

# Find most similar images
similarities = (text_embedding @ image_embeddings.T).squeeze(0)
top_k = similarities.topk(3)

for idx, score in zip(top_k.indices, top_k.values):
    print(f"{image_paths[idx]}: {score:.3f}")
```

## Модерация контента

```python
# Define categories
categories = [
    "safe for work",
    "not safe for work",
    "violent content",
    "graphic content"
]

text = clip.tokenize(categories).to(device)

# Check image
with torch.no_grad():
    logits_per_image, _ = model(image, text)
    probs = logits_per_image.softmax(dim=-1)

# Get classification
max_idx = probs.argmax().item()
max_prob = probs[0, max_idx].item()

print(f"Category: {categories[max_idx]} ({max_prob:.2%})")
```

## Пакетная обработка

```python
# Process multiple images
images = [preprocess(Image.open(f"img{i}.jpg")) for i in range(10)]
images = torch.stack(images).to(device)

with torch.no_grad():
    image_features = model.encode_image(images)
    image_features /= image_features.norm(dim=-1, keepdim=True)

# Batch text
texts = ["a dog", "a cat", "a bird"]
text_tokens = clip.tokenize(texts).to(device)

with torch.no_grad():
    text_features = model.encode_text(text_tokens)
    text_features /= text_features.norm(dim=-1, keepdim=True)

# Similarity matrix (10 images × 3 texts)
similarities = image_features @ text_features.T
print(similarities.shape)  # (10, 3)
```

## Интеграция с векторными базами данных

```python
# Store CLIP embeddings in Chroma/FAISS
import chromadb

client = chromadb.Client()
collection = client.create_collection("image_embeddings")

# Add image embeddings
for img_path, embedding in zip(image_paths, image_embeddings):
    collection.add(
        embeddings=[embedding.cpu().numpy().tolist()],
        metadatas=[{"path": img_path}],
        ids=[img_path]
    )

# Query with text
query = "a sunset"
text_embedding = model.encode_text(clip.tokenize([query]))
results = collection.query(
    query_embeddings=[text_embedding.cpu().numpy().tolist()],
    n_results=5
)
```

## Лучшие практики

1. **В большинстве случаев используйте ViT-B/32** - Хороший баланс
2. **Нормализация вложений** — требуется для косинусного подобия.
3. **Пакетная обработка** – более эффективная.
4. **Внедрение кэша** – дорогостоящие повторные вычисления.
5. **Используйте описательные метки**. Повышение производительности при нулевом выстреле.
6. **Рекомендуется графический процессор** — в 10–50 раз быстрее.
7. **Предварительная обработка изображений** – используйте предусмотренную функцию предварительной обработки.

## Производительность

| Операция | процессор | Графический процессор (V100) |
|-----------|-----|------------|
| Кодирование изображения | ~200 мс | ~20 мс |
| Кодировка текста | ~50 мс | ~5 мс |
| Вычисление сходства | &lt;1 мс | &lt;1 мс |

## Ограничения

1. **Не для мелкомасштабных задач**. Лучше всего подходит для широких категорий.
2. **Требуется описательный текст**. Неясные ярлыки неэффективны.
3. **С предвзятостью на основе веб-данных** – может иметь место предвзятость в наборе данных.
4. **Нет ограничивающих рамок** — только все изображение.
5. **Ограниченное пространственное понимание** – Слабое позиционирование/счет.

## Ресурсы

- **GitHub**: https://github.com/openai/CLIP ⭐ 25 300+
- **Бумага**: https://arxiv.org/abs/2103.00020
- **Colab**: https://colab.research.google.com/github/openai/clip/
- **Лицензия**: MIT