---
title: Шишка — Управляемая векторная БД для производства РАГ и поиска
sidebar_label: Pinecone
description: Управляемая векторная БД для производства РАГ и поиска
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Сосновая шишка

Управляемая векторная БД для производства РАГ и поиска.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/mlops/pinecone` |
| Путь | `optional-skills/mlops/pinecone` |
| Версия | `1.0.1` |
| Автор | Исследование оркестра |
| Лицензия | Массачусетский технологический институт |
| Зависимости | `pinecone` |
| Платформы | Linux, MacOS, Windows |
| Теги | `RAG`, `Pinecone`, `Vector Database`, `Managed Service`, `Serverless`, `Hybrid Search`, `Production`, `Auto-Scaling`, `Low Latency`, `Recommendations` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Сосновая шишка — управляемая база данных векторов

База данных векторов для производственных приложений искусственного интеллекта.

## Когда использовать сосновую шишку

**Используйте, когда:**
- Нужна управляемая бессерверная база данных векторов.
- Производственные приложения RAG
- Требуется автоматическое масштабирование
- Критическая низкая задержка (&lt;100 мс)
- Не хотите управлять инфраструктурой
- Нужен гибридный поиск (плотные + разреженные векторы)

**Показатели**:
- Полностью управляемое SaaS
- Автоматическое масштабирование до миллиардов векторов
- **задержка p95 &lt;100 мс**
- Соглашение об уровне обслуживания 99,9% времени безотказной работы

**Вместо этого используйте альтернативы**:
- **Chroma**: самостоятельное размещение, открытый исходный код.
- **FAISS**: оффлайн, чистый поиск по сходству.
- **Weaviate**: автономный хостинг с большим количеством функций.

## Быстрый старт

### Установка

```bash
pip install pinecone
```

> Примечание: старый пакет `pinecone-client` устарел. Установите `pinecone` (v5+; текущая версия 9.x). Импорт остается `from pinecone import Pinecone`.

### Базовое использование

```python
from pinecone import Pinecone, ServerlessSpec

# Initialize
pc = Pinecone(api_key="your-api-key")

# Create index
pc.create_index(
    name="my-index",
    dimension=1536,  # Must match embedding dimension
    metric="cosine",  # or "euclidean", "dotproduct"
    spec=ServerlessSpec(cloud="aws", region="us-east-1")
)

# Connect to index
index = pc.Index("my-index")

# Upsert vectors
index.upsert(vectors=[
    {"id": "vec1", "values": [0.1, 0.2, ...], "metadata": {"category": "A"}},
    {"id": "vec2", "values": [0.3, 0.4, ...], "metadata": {"category": "B"}}
])

# Query
results = index.query(
    vector=[0.1, 0.2, ...],
    top_k=5,
    include_metadata=True
)

print(results["matches"])
```

## Основные операции

### Создать индекс

```python
# Serverless (recommended)
pc.create_index(
    name="my-index",
    dimension=1536,
    metric="cosine",
    spec=ServerlessSpec(
        cloud="aws",         # or "gcp", "azure"
        region="us-east-1"
    )
)

# Pod-based (for consistent performance)
from pinecone import PodSpec

pc.create_index(
    name="my-index",
    dimension=1536,
    metric="cosine",
    spec=PodSpec(
        environment="us-east1-gcp",
        pod_type="p1.x1"
    )
)
```

### Обновление векторов

```python
# Single upsert
index.upsert(vectors=[
    {
        "id": "doc1",
        "values": [0.1, 0.2, ...],  # 1536 dimensions
        "metadata": {
            "text": "Document content",
            "category": "tutorial",
            "timestamp": "2025-01-01"
        }
    }
])

# Batch upsert (recommended)
vectors = [
    {"id": f"vec{i}", "values": embedding, "metadata": metadata}
    for i, (embedding, metadata) in enumerate(zip(embeddings, metadatas))
]

index.upsert(vectors=vectors, batch_size=100)
```

### Векторы запросов

```python
# Basic query
results = index.query(
    vector=[0.1, 0.2, ...],
    top_k=10,
    include_metadata=True,
    include_values=False
)

# With metadata filtering
results = index.query(
    vector=[0.1, 0.2, ...],
    top_k=5,
    filter={"category": {"$eq": "tutorial"}}
)

# Namespace query
results = index.query(
    vector=[0.1, 0.2, ...],
    top_k=5,
    namespace="production"
)

# Access results
for match in results["matches"]:
    print(f"ID: {match['id']}")
    print(f"Score: {match['score']}")
    print(f"Metadata: {match['metadata']}")
```

### Фильтрация метаданных

```python
# Exact match
filter = {"category": "tutorial"}

# Comparison
filter = {"price": {"$gte": 100}}  # $gt, $gte, $lt, $lte, $ne

# Logical operators
filter = {
    "$and": [
        {"category": "tutorial"},
        {"difficulty": {"$lte": 3}}
    ]
}  # Also: $or

# In operator
filter = {"tags": {"$in": ["python", "ml"]}}
```

## Пространства имен

```python
# Partition data by namespace
index.upsert(
    vectors=[{"id": "vec1", "values": [...]}],
    namespace="user-123"
)

# Query specific namespace
results = index.query(
    vector=[...],
    namespace="user-123",
    top_k=5
)

# List namespaces
stats = index.describe_index_stats()
print(stats['namespaces'])
```

## Гибридный поиск (плотный + разреженный)

```python
# Upsert with sparse vectors
index.upsert(vectors=[
    {
        "id": "doc1",
        "values": [0.1, 0.2, ...],  # Dense vector
        "sparse_values": {
            "indices": [10, 45, 123],  # Token IDs
            "values": [0.5, 0.3, 0.8]   # TF-IDF scores
        },
        "metadata": {"text": "..."}
    }
])

# Hybrid query
# NOTE: index.query() does NOT accept an `alpha` kwarg. Pinecone stores a
# single sparse-dense vector, so weighting must be applied by pre-scaling the
# query vectors before sending them. Use the hybrid_score_norm helper below
# (alpha * dense + (1 - alpha) * sparse; alpha=1 → pure dense, 0 → pure sparse).

def hybrid_score_norm(dense, sparse, alpha: float):
    """Scale dense/sparse query vectors for weighted hybrid search."""
    if not 0 <= alpha <= 1:
        raise ValueError("alpha must be between 0 and 1")
    scaled_sparse = {
        "indices": sparse["indices"],
        "values": [v * (1 - alpha) for v in sparse["values"]],
    }
    return [v * alpha for v in dense], scaled_sparse

hdense, hsparse = hybrid_score_norm(
    dense=[0.1, 0.2, ...],
    sparse={"indices": [10, 45], "values": [0.5, 0.3]},
    alpha=0.5,  # 0=sparse, 1=dense, 0.5=balanced
)

results = index.query(
    vector=hdense,
    sparse_vector=hsparse,
    top_k=5,
)
```

## Интеграция с LangChain

```python
from langchain_pinecone import PineconeVectorStore
from langchain_openai import OpenAIEmbeddings

# Create vector store
vectorstore = PineconeVectorStore.from_documents(
    documents=docs,
    embedding=OpenAIEmbeddings(),
    index_name="my-index"
)

# Query
results = vectorstore.similarity_search("query", k=5)

# With metadata filter
results = vectorstore.similarity_search(
    "query",
    k=5,
    filter={"category": "tutorial"}
)

# As retriever
retriever = vectorstore.as_retriever(search_kwargs={"k": 10})
```

## Интеграция LlamaIndex

```python
from llama_index.vector_stores.pinecone import PineconeVectorStore

# Connect to Pinecone
pc = Pinecone(api_key="your-key")
pinecone_index = pc.Index("my-index")

# Create vector store
vector_store = PineconeVectorStore(pinecone_index=pinecone_index)

# Use in LlamaIndex
from llama_index.core import StorageContext, VectorStoreIndex

storage_context = StorageContext.from_defaults(vector_store=vector_store)
index = VectorStoreIndex.from_documents(documents, storage_context=storage_context)
```

## Управление индексами

```python
# List indices
indexes = pc.list_indexes()

# Describe index
index_info = pc.describe_index("my-index")
print(index_info)

# Get index stats
stats = index.describe_index_stats()
print(f"Total vectors: {stats['total_vector_count']}")
print(f"Namespaces: {stats['namespaces']}")

# Delete index
pc.delete_index("my-index")
```

## Удалить векторы

```python
# Delete by ID
index.delete(ids=["vec1", "vec2"])

# Delete by filter
index.delete(filter={"category": "old"})

# Delete all in namespace
index.delete(delete_all=True, namespace="test")

# Delete entire index
index.delete(delete_all=True)
```

## Лучшие практики

1. **Используйте бессерверные решения** — автоматическое масштабирование, экономичность.
2. **Пакетная установка** – более эффективна (100–200 на партию).
3. **Добавить метаданные** – включить фильтрацию.
4. **Использовать пространства имен** – изолировать данные по пользователю/арендатору.
5. **Отслеживание использования** – проверьте панель управления Pinecone.
6. **Оптимизация фильтров** – индексирование часто фильтруемых полей.
7. **Тестирование с бесплатным уровнем** — 1 индекс, 100 тысяч векторов бесплатно.
8. **Используйте гибридный поиск** – лучшее качество.
9. **Установите соответствующие размеры** – сопоставьте модель внедрения.
10. **Регулярное резервное копирование**. Экспортируйте важные данные.

## Производительность

| Операция | Задержка | Заметки |
|-----------|---------|-------|
| Упсерт | ~50-100мс | За партию |
| Запрос (стр. 50) | ~50 мс | Зависит от размера индекса |
| Запрос (стр. 95) | ~100 мс | цель SLA |
| Фильтр метаданных | ~+10-20мс | Дополнительные накладные расходы |

## Цены (по состоянию на 2025 г.)

**Бессерверная**:
- 0,096 доллара США за миллион единиц чтения
- 0,06 доллара США за миллион единиц записи
- 0,06 доллара США за ГБ хранилища в месяц.

**Уровень бесплатного пользования**:
- 1 бессерверный индекс
- 100 тыс. векторов (1536 измерений)
- Отлично подходит для прототипирования

## Ресурсы

- **Веб-сайт**: https://www.pinecone.io.
- **Документация**: https://docs.pinecone.io.
- **Консоль**: https://app.pinecone.io.
- **Цены**: https://www.pinecone.io/pricing.