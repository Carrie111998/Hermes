---
name: pinecone
description: 'Pinecone: fully managed serverless vector DB, API key only - no ops, auto-scaling, hybrid dense+sparse search and namespaces for production RAG at scale.'
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [pinecone-client]
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [RAG, Pinecone, Vector Database, Managed Service, Serverless, Hybrid Search, Production, Auto-Scaling, Low Latency, Recommendations]

---

# Pinecone - Managed Vector Database

The vector database for production AI applications.

## When to use / when NOT to use

Use Pinecone when you want **zero ops**: it is a fully managed cloud service reached with
an API key only — nothing to install, auto-scaling to billions of vectors, p95 <100ms.
Pick it for production RAG where you do not want to own a server.

Do NOT use it when you need self-hosting or data residency on your own machines: use
**qdrant** for a self-hosted vector DB server you run (Docker/binary/cloud), **chroma**
for an embedded in-process DB persisting to a local directory, **faiss** for a pure
in-process index library with no service and no metadata store.

## Routing table

| To do X | Read |
|---------|------|
| Create/upsert/query/delete via the client, all parameters and filter operators | `references/client-api.md` |
| Choose serverless vs pod-based, hybrid dense+sparse search, namespaces for multi-tenancy, metadata filter recipes, latency/pricing, operational best practices | `references/deployment.md` |
| Wire Pinecone into LangChain or LlamaIndex | `references/integration.md` |

## Key constraints

- `dimension` is fixed at index creation and must match the embedding model exactly.
- `metric` is one of `cosine`, `euclidean`, `dotproduct` — also immutable.
- Batch upserts in chunks of 100-200 vectors; single-vector upserts waste write units.
- `include_metadata=True` is required to get metadata back; values are omitted by default.
- Namespaces are the isolation unit — a query only ever sees one namespace.
- API key is a secret: load from the environment, never hardcode it.

## End-to-end skeleton

```python
from pinecone import Pinecone, ServerlessSpec
import os

pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])

pc.create_index(
    name="my-index",
    dimension=1536,
    metric="cosine",
    spec=ServerlessSpec(cloud="aws", region="us-east-1"),
)
index = pc.Index("my-index")

index.upsert(vectors=[
    {"id": "vec1", "values": [0.1, 0.2, ...], "metadata": {"category": "A"}},
    {"id": "vec2", "values": [0.3, 0.4, ...], "metadata": {"category": "B"}},
])

results = index.query(vector=[0.1, 0.2, ...], top_k=5, include_metadata=True)
print(results["matches"])
```
