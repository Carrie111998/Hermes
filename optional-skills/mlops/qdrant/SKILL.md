---
name: qdrant-vector-search
description: 'Qdrant: self-hosted vector DB server (Docker/binary/cloud) with rich payload filtering - Rust HNSW, quantization, distributed sharding for production RAG that needs a real service, not an in-process index.'
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [qdrant-client>=1.12.0]
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [RAG, Vector Search, Qdrant, Semantic Search, Embeddings, Similarity Search, HNSW, Production, Distributed]

---

# Qdrant - Vector Similarity Search Engine

High-performance vector database written in Rust for production RAG and semantic search.

## When to use / when NOT to use

Use Qdrant when you need a **real vector DB service you host yourself** — Docker, binary
or Qdrant Cloud — with rich payload filtering during search, quantization, and distributed
sharding/replication. Right choice for production RAG, hybrid dense+sparse search and
real-time recommendations where data stays under your control.

Do NOT use it if you do not want to run a service: **chroma** is an embedded in-process DB
persisting to a local directory, **faiss** is an in-process index library with no server
and no metadata store, **pinecone** is a fully managed serverless cloud DB (API key only).
(Weaviate is the alternative if you specifically want GraphQL and built-in vectorizers.)

## Routing table

| To do X | Read |
|---------|------|
| Install, run via Docker, connect (incl. Cloud / gRPC / timeouts), tune HNSW and optimizer, operational best practices | `references/setup-and-deployment.md` |
| Points, collections, distance metrics, search/filtered/batch search, payload indexing, named + sparse vectors, quantization config | `references/client-api.md` |
| Index documents with sentence-transformers, LangChain or LlamaIndex | `references/rag-integration.md` |
| Distributed clusters, sharding, replication/consistency, RRF hybrid search, recommendations, geo/nested/full-text filters, quantization strategies, snapshots, aliases, scroll, async/gRPC clients, multitenancy, monitoring | `references/advanced-usage.md` |
| Diagnose install/connection/collection/search/upsert/memory/cluster failures and benchmark configs | `references/troubleshooting.md` |

## Key constraints

- Vector `size` and `distance` are fixed per collection; a dimension mismatch on upsert
  is a hard error — recreate the collection to change them.
- Any payload field used in a filter needs `create_payload_index`, otherwise filtered
  search degrades to a scan.
- Point IDs must be integers or UUID strings — nothing else is accepted.
- Payload must be JSON-serializable: no `datetime`, numpy arrays or custom objects.
- Always batch upserts; use `wait=True` only when you must read back immediately.
- Enable quantization plus `on_disk_payload` before collections exceed ~1M vectors;
  shard beyond ~10M.

## End-to-end skeleton

```python
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

client = QdrantClient(host="localhost", port=6333)

client.create_collection(
    collection_name="documents",
    vectors_config=VectorParams(size=384, distance=Distance.COSINE),
)

client.upsert(
    collection_name="documents",
    points=[
        PointStruct(id=1, vector=[0.1, 0.2, ...], payload={"title": "Doc 1", "category": "tech"}),
        PointStruct(id=2, vector=[0.3, 0.4, ...], payload={"title": "Doc 2", "category": "science"}),
    ],
)

results = client.search(
    collection_name="documents",
    query_vector=[0.15, 0.25, ...],
    query_filter={"must": [{"key": "category", "match": {"value": "tech"}}]},
    limit=10,
)

for point in results:
    print(f"ID: {point.id}, Score: {point.score}, Payload: {point.payload}")
```
