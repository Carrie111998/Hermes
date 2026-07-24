---
name: chroma
description: 'Chroma: embedded in-process vector DB, zero server needed - persists to a local directory, 4-function API, ideal for notebooks, prototypes and small self-hosted RAG.'
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [chromadb, sentence-transformers]
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [RAG, Chroma, Vector Database, Embeddings, Semantic Search, Open Source, Self-Hosted, Document Retrieval, Metadata Filtering]

---

# Chroma - Open-Source Embedding Database

The AI-native database for building LLM applications with memory.

## When to use / when NOT to use

Use Chroma when you want a vector DB **inside your Python process** with no server to
run: `PersistentClient(path=...)` writes to a local directory, the API is four functions
(add/query/get/delete), and documents plus metadata live alongside the vectors. Ideal for
notebooks, prototypes and small self-hosted RAG.

Do NOT use it as a shared production service: use **qdrant** for a self-hosted vector DB
server (Docker/binary/cloud) with rich payload filtering, **pinecone** for a fully managed
serverless cloud DB, **faiss** for a raw in-process index library when you need maximum
speed and no metadata store.

## Routing table

| To do X | Read |
|---------|------|
| Full client/collection API — create, add, query, get, update, delete, persistence, server mode, best practices, latency numbers | `references/client-api.md` |
| Pick or write an embedding function (default sentence-transformers, OpenAI, HuggingFace, custom) | `references/embedding-functions.md` |
| Write `where` metadata filters (`$gt`, `$and`, `$in`, …) | `references/metadata-filtering.md` |
| Wire Chroma into LangChain or LlamaIndex | `references/integration.md` |

## Key constraints

- `chromadb.Client()` is in-memory and loses everything on exit — use
  `chromadb.PersistentClient(path="./chroma_db")` for anything you want to keep.
- The embedding function is fixed per collection; reopening with a different one silently
  produces meaningless distances.
- `ids` must be unique strings; re-adding an existing id raises instead of updating (use
  `update`/`upsert`).
- Metadata values must be scalars — no nested objects.
- `query` returns parallel lists keyed by `documents` / `metadatas` / `distances` / `ids`,
  one entry per query text.

## End-to-end skeleton

```python
import chromadb

client = chromadb.PersistentClient(path="./chroma_db")
collection = client.get_or_create_collection(name="my_collection")

collection.add(
    documents=["This is document 1", "This is document 2"],
    metadatas=[{"source": "doc1"}, {"source": "doc2"}],
    ids=["id1", "id2"],
)

results = collection.query(query_texts=["document about topic"], n_results=2)
print(results["documents"], results["distances"])
```
