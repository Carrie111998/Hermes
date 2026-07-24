# Chroma Client API

Complete reference for the Chroma client and collection API: installation, collection lifecycle, add/query/get/update/delete, persistence and server mode.

## Installation

```bash
# Python
pip install chromadb

# JavaScript/TypeScript
npm install chromadb @chroma-core/default-embed
```

## 1. Create collection

```python
import chromadb

client = chromadb.Client()

# Simple collection
collection = client.create_collection("my_docs")

# With custom embedding function
from chromadb.utils import embedding_functions

openai_ef = embedding_functions.OpenAIEmbeddingFunction(
    api_key="your-key",
    model_name="text-embedding-3-small"
)

collection = client.create_collection(
    name="my_docs",
    embedding_function=openai_ef
)

# Get existing collection
collection = client.get_collection("my_docs")

# Delete collection
client.delete_collection("my_docs")
```

`get_or_create_collection(name)` is the idempotent variant used in server mode and
LlamaIndex wiring.

Embedding-function options are catalogued in
[embedding-functions.md](embedding-functions.md).

## 2. Add documents

```python
# Add with explicit IDs
collection.add(
    documents=["Doc 1", "Doc 2", "Doc 3"],
    metadatas=[
        {"source": "web", "category": "tutorial"},
        {"source": "pdf", "page": 5},
        {"source": "api", "timestamp": "2025-01-01"}
    ],
    ids=["id1", "id2", "id3"]
)

# Add with custom (pre-computed) embeddings
collection.add(
    embeddings=[[0.1, 0.2, ...], [0.3, 0.4, ...]],
    documents=["Doc 1", "Doc 2"],
    ids=["id1", "id2"]
)
```

## 3. Query (similarity search)

```python
# Basic query
results = collection.query(
    query_texts=["machine learning tutorial"],
    n_results=5
)

# Query with filters
results = collection.query(
    query_texts=["Python programming"],
    n_results=3,
    where={"source": "web"}
)

# Query with compound metadata filters
results = collection.query(
    query_texts=["advanced topics"],
    where={
        "$and": [
            {"category": "tutorial"},
            {"difficulty": {"$gte": 3}}
        ]
    }
)

# Access results
print(results["documents"])      # List of matching documents
print(results["metadatas"])      # Metadata for each doc
print(results["distances"])      # Similarity scores
print(results["ids"])            # Document IDs
```

The full `where` DSL is in [metadata-filtering.md](metadata-filtering.md).

## 4. Get documents

```python
# Get by IDs
docs = collection.get(
    ids=["id1", "id2"]
)

# Get with filters
docs = collection.get(
    where={"category": "tutorial"},
    limit=10
)

# Get all documents
docs = collection.get()
```

## 5. Update documents

```python
collection.update(
    ids=["id1"],
    documents=["Updated content"],
    metadatas=[{"source": "updated"}]
)
```

## 6. Delete documents

```python
# Delete by IDs
collection.delete(ids=["id1", "id2"])

# Delete with filter
collection.delete(
    where={"source": "outdated"}
)
```

## Persistent storage

```python
# Persist to disk
client = chromadb.PersistentClient(path="./chroma_db")

collection = client.create_collection("my_docs")
collection.add(documents=["Doc 1"], ids=["id1"])

# Data persisted automatically
# Reload later with same path
client = chromadb.PersistentClient(path="./chroma_db")
collection = client.get_collection("my_docs")
```

`chromadb.Client()` is in-memory only — data is lost on process exit.

## Server mode

```python
# Run Chroma server
# Terminal: chroma run --path ./chroma_db --port 8000

# Connect to server
import chromadb
from chromadb.config import Settings

client = chromadb.HttpClient(
    host="localhost",
    port=8000,
    settings=Settings(anonymized_telemetry=False)
)

# Use as normal
collection = client.get_or_create_collection("my_docs")
```

## Operational best practices

1. **Use persistent client** - Don't lose data on restart
2. **Add metadata** - Enables filtering and tracking
3. **Batch operations** - Add multiple docs at once
4. **Choose right embedding model** - Balance speed/quality
5. **Use filters** - Narrow search space
6. **Unique IDs** - Avoid collisions
7. **Regular backups** - Copy chroma_db directory
8. **Monitor collection size** - Scale up if needed
9. **Test embedding functions** - Ensure quality
10. **Use server mode for production** - Better for multi-user

## Performance

| Operation | Latency | Notes |
|-----------|---------|-------|
| Add 100 docs | ~1-3s | With embedding |
| Query (top 10) | ~50-200ms | Depends on collection size |
| Metadata filter | ~10-50ms | Fast with proper indexing |

## Project metrics

- **24,300+ GitHub stars**, 1,900+ forks
- **v1.3.3+** (stable, weekly releases)
- **Apache 2.0 license**
- GitHub: https://github.com/chroma-core/chroma
- Discord: https://discord.gg/MMeYNTmh3x
