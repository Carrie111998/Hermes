---
title: Faiss — Быстрый поиск сходства векторов в миллиардном масштабе
sidebar_label: Faiss
description: Быстрый поиск сходства векторов в миллиардном масштабе
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Фейсс

Быстрый поиск сходства векторов в миллиардном масштабе.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/mlops/faiss` |
| Путь | `optional-skills/mlops/faiss` |
| Версия | `1.0.0` |
| Автор | Исследование оркестра |
| Лицензия | Массачусетский технологический институт |
| Зависимости | `faiss-cpu`, `faiss-gpu`, `numpy` |
| Платформы | Linux, MacOS |
| Теги | `RAG`, `FAISS`, `Similarity Search`, `Vector Search`, `Facebook AI`, `GPU Acceleration`, `Billion-Scale`, `K-NN`, `HNSW`, `High Performance`, `Large Scale` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# FAISS — эффективный поиск по сходству

Библиотека искусственного интеллекта Facebook для поиска сходства векторов в миллиардах масштабов.

## Когда использовать FAISS

**Используйте FAISS, когда:**
- Нужен быстрый поиск сходства в больших наборах векторных данных (миллионы/миллиарды)
- Требуется ускорение графического процессора
- Чистое векторное сходство (фильтрация метаданных не требуется)
- Высокая пропускная способность, критическая низкая задержка
- Автономная/пакетная обработка вложений

**Показатели**:
- **31 700+ звезд GitHub**
- Мета/исследование искусственного интеллекта в Facebook
- **Обрабатывает миллиарды векторов**
- **C++** с привязками Python

**Вместо этого используйте альтернативы**:
- **Цветность/Сосновая шишка**: требуется фильтрация метаданных.
- **Weaviate**: нужны полные функции базы данных.
- **Раздражает**: проще, меньше функций.

## Быстрый старт

### Установка

```bash
# CPU only
pip install faiss-cpu

# GPU support
pip install faiss-gpu
```

### Базовое использование

```python
import faiss
import numpy as np

# Create sample data (1000 vectors, 128 dimensions)
d = 128
nb = 1000
vectors = np.random.random((nb, d)).astype('float32')

# Create index
index = faiss.IndexFlatL2(d)  # L2 distance
index.add(vectors)             # Add vectors

# Search
k = 5  # Find 5 nearest neighbors
query = np.random.random((1, d)).astype('float32')
distances, indices = index.search(query, k)

print(f"Nearest neighbors: {indices}")
print(f"Distances: {distances}")
```

## Типы индексов

### 1. Квартира (точный поиск)

```python
# L2 (Euclidean) distance
index = faiss.IndexFlatL2(d)

# Inner product (cosine similarity if normalized)
index = faiss.IndexFlatIP(d)

# Slowest, most accurate
```

### 2. ЭКО (инвертированный файл) – Быстрое приближение

```python
# Create quantizer
quantizer = faiss.IndexFlatL2(d)

# IVF index with 100 clusters
nlist = 100
index = faiss.IndexIVFFlat(quantizer, d, nlist)

# Train on data
index.train(vectors)

# Add vectors
index.add(vectors)

# Search (nprobe = clusters to search)
index.nprobe = 10
distances, indices = index.search(query, k)
```

### 3. HNSW (Иерархический NSW) — лучшее качество/скорость.

```python
# HNSW index
M = 32  # Number of connections per layer
index = faiss.IndexHNSWFlat(d, M)

# No training needed
index.add(vectors)

# Search
distances, indices = index.search(query, k)
```

### 4. Квантование продукта — эффективное использование памяти

```python
# PQ reduces memory by 16-32×
m = 8   # Number of subquantizers
nbits = 8
index = faiss.IndexPQ(d, m, nbits)

# Train and add
index.train(vectors)
index.add(vectors)
```

## Сохраняем и загружаем

```python
# Save index
faiss.write_index(index, "large.index")

# Load index
index = faiss.read_index("large.index")

# Continue using
distances, indices = index.search(query, k)
```

## ускорение графического процессора

```python
# Single GPU
res = faiss.StandardGpuResources()
index_cpu = faiss.IndexFlatL2(d)
index_gpu = faiss.index_cpu_to_gpu(res, 0, index_cpu)  # GPU 0

# Multi-GPU
index_gpu = faiss.index_cpu_to_all_gpus(index_cpu)

# 10-100× faster than CPU
```

## Интеграция с LangChain

```python
from langchain_community.vectorstores import FAISS
from langchain_openai import OpenAIEmbeddings

# Create FAISS vector store
vectorstore = FAISS.from_documents(docs, OpenAIEmbeddings())

# Save
vectorstore.save_local("faiss_index")

# Load
vectorstore = FAISS.load_local(
    "faiss_index",
    OpenAIEmbeddings(),
    allow_dangerous_deserialization=True
)

# Search
results = vectorstore.similarity_search("query", k=5)
```

## Интеграция LlamaIndex

```python
from llama_index.vector_stores.faiss import FaissVectorStore
import faiss

# Create FAISS index
d = 1536
faiss_index = faiss.IndexFlatL2(d)

vector_store = FaissVectorStore(faiss_index=faiss_index)
```

## Лучшие практики

1. **Выберите правильный тип индекса** — Flat для &lt;10K, IVF для 10K–1M, HNSW для качества.
2. **Нормализация косинуса** – используйте IndexFlatIP с нормализованными векторами.
3. **Используйте графический процессор для больших наборов данных** — в 10–100 раз быстрее.
4. **Сохраняйте обученные индексы**. Обучение стоит дорого.
5. **Tune nprobe/ef_search** — скорость/точность баланса.
6. **Мониторинг памяти** — PQ для больших наборов данных.
7. **Пакетные запросы** — лучшее использование графического процессора.

## Производительность

| Тип индекса | Время сборки | Время поиска | Память | Точность |
|------------|------------|-------------|--------|----------|
| Квартира | Быстро | Медленно | Высокий | 100% |
| ЭКО | Средний | Быстро | Средний | 95-99% |
| HNSW | Медленно | Самый быстрый | Высокий | 99% |
| ПК | Средний | Быстро | Низкий | 90-95% |

## Ресурсы

- **GitHub**: https://github.com/facebookresearch/faiss ⭐ 31 700+
- **Вики**: https://github.com/facebookresearch/faiss/wiki
- **Лицензия**: MIT