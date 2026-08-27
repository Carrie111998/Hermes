---
title: 'Nemo Curator — Curate LLM training data: dedupe, filter, PII redaction'
sidebar_label: Nemo Curator
description: 'Курировать данные обучения LLM: дедупликация, фильтрация, редактирование
  личных данных'
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Немо Куратор

Курировать данные обучения LLM: дедупликация, фильтрация, редактирование личных данных.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/mlops/nemo-curator` |
| Путь | `optional-skills/mlops/nemo-curator` |
| Версия | `1.0.1` |
| Автор | Исследование оркестра |
| Лицензия | Массачусетский технологический институт |
| Зависимости | `nemo-curator`, `cudf`, `dask`, `rapids` |
| Платформы | Linux, MacOS |
| Теги | `Data Processing`, `NeMo Curator`, `Data Curation`, `GPU Acceleration`, `Deduplication`, `Quality Filtering`, `NVIDIA`, `RAPIDS`, `PII Redaction`, `Multimodal`, `LLM Training Data` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# NeMo Curator — Курирование данных с ускорением на графическом процессоре

Набор инструментов NVIDIA для подготовки высококачественных обучающих данных для программ LLM.

## Когда использовать NeMo Curator

**Используйте NeMo Curator, когда:**
- Подготовка данных обучения LLM из веб-страниц (обычное сканирование)
- Требуется быстрая дедупликация (в 16 раз быстрее, чем процессор)
- Курирование мультимодальных наборов данных (текст, изображения, видео, аудио)
- Фильтрация некачественного или токсичного контента.
- Масштабирование обработки данных в кластере графических процессоров.

**Производительность**:
- **в 16 раз быстрее** нечеткая дедупликация (8 ТБ RedPajama v2)
- **Совокупная стоимость владения на 40 % ниже** по сравнению с альтернативными процессорами
- **Почти линейное масштабирование** по узлам графического процессора.

**Вместо этого используйте альтернативы**:
- **datatrove**: обработка данных с открытым исходным кодом на базе ЦП.
- **dolma**: набор инструментов для работы с данными Allen AI.
– **Ray Data**: общая обработка данных машинного обучения (без курирования).

## Быстрый старт

### Установка

```bash
# NeMo Curator 1.x installs with uv. Extras use hyphens (PyPI-normalized):
#   text-cuda12 / text-cpu (and image/video/audio/math variants), or `all`.

# Text curation (CUDA 12)
uv pip install "nemo-curator[text-cuda12]"

# All modalities
uv pip install "nemo-curator[all]"

# CPU-only text (slower)
uv pip install "nemo-curator[text-cpu]"
```

### Базовый конвейер курирования текста

> **Основная переработка версии (1.x):** NeMo Curator был переписан на основе **основанного на Ray
> архитектура конвейера/этапа**. Старый `DocumentDataset` + `nemo_curator.modules.*` /
> `ScoreFilter` / `Modify` API вызова объекта в наборе данных из версии 0.x больше не существует. В 1.x вы
> скомпонуйте `ProcessingStage`s в `Pipeline` и запустите его с помощью исполнителя. Точный
> поверхность сцены/импорта различается в зависимости от модальности — рассматривайте примеры в этом навыке ниже как
> **концептуальный** (стиль 0.x) и следуйте текущим
> [быстрый старт](https://github.com/NVIDIA-NeMo/Curator/blob/main/tutorials/quickstart.py)
> и [текстовое руководство](https://docs.nvidia.com/nemo/curator/latest/get-started/text) для
> точные API 1.x, а не дословное копирование импорта.

Форма конвейера версии 1.x (из краткого руководства по восходящей разработке):

```python
from nemo_curator.pipeline import Pipeline
from nemo_curator.stages.base import ProcessingStage
from nemo_curator.stages.resources import Resources
from nemo_curator.backends.xenna import XennaExecutor
from nemo_curator.core.client import RayClient

# 1. Define/compose stages (load -> filter -> dedupe -> classify -> write).
#    Each stage declares its own Resources (CPU cores, GPU memory, replicas).
pipeline = Pipeline(name="curation", stages=[...])

# 2. Run it with an executor (Ray-backed).
client = RayClient()
client.start()
pipeline.run(XennaExecutor())
client.stop()
```

Фрагменты в стиле 0.x в последующих разделах иллюстрируют *концепции* (качество
фильтрация, точная/нечеткая/семантическая дедупликация, редактирование PII, фильтрация классификаторов). Для работоспособности
1.x сопоставьте каждую концепцию с соответствующим этапом из руководства по модальности.

## Конвейер обработки данных

### Этап 1. Качественная фильтрация

```python
from nemo_curator.filters import (
    WordCountFilter,
    RepeatedLinesFilter,
    UrlRatioFilter,
    NonAlphaNumericFilter
)

# Apply 30+ heuristic filters
from nemo_curator import ScoreFilter

# Word count filter
dataset = dataset.filter(WordCountFilter(min_words=50, max_words=100000))

# Remove repetitive content
dataset = dataset.filter(RepeatedLinesFilter(max_repeated_line_fraction=0.3))

# URL ratio filter
dataset = dataset.filter(UrlRatioFilter(max_url_ratio=0.2))
```

### Этап 2. Дедупликация

**Точная дедупликация**:
```python
from nemo_curator.modules import ExactDuplicates

# Remove exact duplicates
deduped = ExactDuplicates(id_field="id", text_field="text")(dataset)
```

**Нечеткая дедупликация** (в 16 раз быстрее на графическом процессоре):
```python
from nemo_curator.modules import FuzzyDuplicates

# MinHash + LSH deduplication
fuzzy_dedup = FuzzyDuplicates(
    id_field="id",
    text_field="text",
    num_hashes=260,      # MinHash parameters
    num_buckets=20,
    hash_method="md5"
)

deduped = fuzzy_dedup(dataset)
```

**Семантическая дедупликация**:
```python
from nemo_curator.modules import SemanticDuplicates

# Embedding-based deduplication
semantic_dedup = SemanticDuplicates(
    id_field="id",
    text_field="text",
    embedding_model="sentence-transformers/all-MiniLM-L6-v2",
    threshold=0.8  # Cosine similarity threshold
)

deduped = semantic_dedup(dataset)
```

### Этап 3. Редактирование личных данных

```python
from nemo_curator.modules import Modify
from nemo_curator.modifiers import PIIRedactor

# Redact personally identifiable information
pii_redactor = PIIRedactor(
    supported_entities=["EMAIL_ADDRESS", "PHONE_NUMBER", "PERSON", "LOCATION"],
    anonymize_action="replace"  # or "redact"
)

redacted = Modify(pii_redactor)(dataset)
```

### Этап 4. Фильтрация классификатора

```python
from nemo_curator.classifiers import QualityClassifier

# Quality classification
quality_clf = QualityClassifier(
    model_path="nvidia/quality-classifier-deberta",
    batch_size=256,
    device="cuda"
)

# Filter low-quality documents
high_quality = dataset.filter(lambda doc: quality_clf(doc["text"]) > 0.5)
```

## ускорение графического процессора

### Производительность графического процессора и процессора

| Операция | ЦП (16 ядер) | Графический процессор (А100) | Ускорение |
|-----------|----------------|------------|---------|
| Нечеткая дедупликация (8 ТБ) | 120 часов | 7,5 часов | 16× |
| Точная дедупликация (1 ТБ) | 8 часов | 0,5 часа | 16× |
| Качественная фильтрация | 2 часа | 0,2 часа | 10 × |

### Масштабирование нескольких графических процессоров

```python
from nemo_curator import get_client
import dask_cuda

# Initialize GPU cluster
client = get_client(cluster_type="gpu", n_workers=8)

# Process with 8 GPUs
deduped = FuzzyDuplicates(...)(dataset)
```

## Мультимодальное курирование

### Курирование изображений

```python
from nemo_curator.image import (
    AestheticFilter,
    NSFWFilter,
    CLIPEmbedder
)

# Aesthetic scoring
aesthetic_filter = AestheticFilter(threshold=5.0)
filtered_images = aesthetic_filter(image_dataset)

# NSFW detection
nsfw_filter = NSFWFilter(threshold=0.9)
safe_images = nsfw_filter(filtered_images)

# Generate CLIP embeddings
clip_embedder = CLIPEmbedder(model="openai/clip-vit-base-patch32")
image_embeddings = clip_embedder(safe_images)
```

### Курирование видео

```python
from nemo_curator.video import (
    SceneDetector,
    ClipExtractor,
    InternVideo2Embedder
)

# Detect scenes
scene_detector = SceneDetector(threshold=27.0)
scenes = scene_detector(video_dataset)

# Extract clips
clip_extractor = ClipExtractor(min_duration=2.0, max_duration=10.0)
clips = clip_extractor(scenes)

# Generate embeddings
video_embedder = InternVideo2Embedder()
video_embeddings = video_embedder(clips)
```

### Аудио курирование

```python
from nemo_curator.audio import (
    ASRInference,
    WERFilter,
    DurationFilter
)

# ASR transcription
asr = ASRInference(model="nvidia/stt_en_fastconformer_hybrid_large_pc")
transcribed = asr(audio_dataset)

# Filter by WER (word error rate)
wer_filter = WERFilter(max_wer=0.3)
high_quality_audio = wer_filter(transcribed)

# Duration filtering
duration_filter = DurationFilter(min_duration=1.0, max_duration=30.0)
filtered_audio = duration_filter(high_quality_audio)
```

## Общие шаблоны

### Курирование веб-скрапинга (обычное сканирование)

```python
from nemo_curator import ScoreFilter, Modify
from nemo_curator.filters import *
from nemo_curator.modules import *
from nemo_curator.datasets import DocumentDataset

# Load Common Crawl data
dataset = DocumentDataset.read_parquet("common_crawl/*.parquet")

# Pipeline
pipeline = [
    # 1. Quality filtering
    WordCountFilter(min_words=100, max_words=50000),
    RepeatedLinesFilter(max_repeated_line_fraction=0.2),
    SymbolToWordRatioFilter(max_symbol_to_word_ratio=0.3),
    UrlRatioFilter(max_url_ratio=0.3),

    # 2. Language filtering
    LanguageIdentificationFilter(target_languages=["en"]),

    # 3. Deduplication
    ExactDuplicates(id_field="id", text_field="text"),
    FuzzyDuplicates(id_field="id", text_field="text", num_hashes=260),

    # 4. PII redaction
    PIIRedactor(),

    # 5. NSFW filtering
    NSFWClassifier(threshold=0.8)
]

# Execute
for stage in pipeline:
    dataset = stage(dataset)

# Save
dataset.to_parquet("curated_common_crawl/")
```

### Распределенная обработка

```python
from nemo_curator import get_client
from dask_cuda import LocalCUDACluster

# Multi-GPU cluster
cluster = LocalCUDACluster(n_workers=8)
client = get_client(cluster=cluster)

# Process large dataset
dataset = DocumentDataset.read_parquet("s3://large_dataset/*.parquet")
deduped = FuzzyDuplicates(...)(dataset)

# Cleanup
client.close()
cluster.close()
```

## Тесты производительности

### Нечеткая дедупликация (RedPajama v2, 8 ТБ)

- **ЦП (256 ядер)**: 120 часов
- **ГП (8 × A100)**: 7,5 часов
- **Ускорение**: 16×

### Точная дедупликация (1 ТБ)

- **ЦП (64 ядра)**: 8 часов
- **ГП (4 × A100)**: 0,5 часа
- **Ускорение**: 16×

### Фильтрация качества (100 ГБ)

- **ЦП (32 ядра)**: 2 часа
- **ГП (2× A100)**: 0,2 часа
- **Ускорение**: 10×

## Сравнение затрат

**Курирование на базе ЦП** (AWS c5.18xlarge × 10):
- Стоимость: 3,60 доллара США в час × 10 = 36 долларов США в час.
- Время на 8 ТБ: 120 часов
- **Всего**: 4320 долларов США.

**Курирование на основе графического процессора** (AWS p4d.24xlarge × 2):
- Стоимость: 32,77 доллара США в час × 2 = 65,54 доллара США в час.
- Время на 8ТБ: 7,5 часов
- **Итого**: 491,55 доллара США.

**Экономия**: скидка 89 % (экономия 3828 долларов США).

## Поддерживаемые форматы данных

- **Ввод**: паркет, JSONL, CSV.
- **Вывод**: паркет (рекомендуется), JSONL.
- **WebDataset**: архивы TAR для мультимодальных изображений.

## Варианты использования

**Производственное развертывание**:
- NVIDIA использовала NeMo Curator для подготовки данных обучения Nemotron-4.
- Курируются наборы данных с открытым исходным кодом: RedPajama v2, The Pile.

## Ссылки

- **[Руководство по фильтрации](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/nemo-curator/references/filtering.md)** - Более 30 качественных фильтров, эвристика
- **[Руководство по дедупликации](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/nemo-curator/references/deduplication.md)** - Точные, нечеткие, семантические методы

## Ресурсы

- **GitHub**: https://github.com/NVIDIA-NeMo/Curator
- **Документация**: https://docs.nvidia.com/nemo/curator/latest/
- **Версия**: 1.2.0 (1.x — это переписанный конвейер на основе Ray — перед копированием фрагментов версии 0.x ознакомьтесь с кратким руководством).
- **Лицензия**: Apache 2.0.