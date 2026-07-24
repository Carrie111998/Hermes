# Qdrant Setup and Deployment

How to install and connect to Qdrant (pip client, Docker, Qdrant Cloud), plus connection options, HNSW/optimizer tuning and operational best practices.

## Installation

```bash
# Python client
pip install qdrant-client

# Docker (recommended for development)
docker run -p 6333:6333 -p 6334:6334 qdrant/qdrant

# Docker with persistent storage
docker run -p 6333:6333 -p 6334:6334 \
    -v $(pwd)/qdrant_storage:/qdrant/storage \
    qdrant/qdrant
```

Port 6333 serves REST, 6334 serves gRPC. Both APIs have full feature parity.

## Connect

```python
from qdrant_client import QdrantClient

# Local / self-hosted
client = QdrantClient(host="localhost", port=6333)

# Tuned connection: timeout + gRPC transport
client = QdrantClient(
    host="localhost",
    port=6333,
    timeout=30,
    prefer_grpc=True  # gRPC for better performance
)

# Qdrant Cloud
client = QdrantClient(
    url="https://your-cluster.cloud.qdrant.io",
    api_key="your-api-key"
)
```

## Performance tuning

```python
from qdrant_client.models import HnswConfigDiff

# Optimize for search speed (higher recall)
client.update_collection(
    collection_name="documents",
    hnsw_config=HnswConfigDiff(ef_construct=200, m=32)
)

# Optimize for indexing speed (bulk loads)
client.update_collection(
    collection_name="documents",
    optimizer_config={"indexing_threshold": 20000}
)
```

Deeper HNSW parameter sets, benchmark configurations and cluster tuning live in
[troubleshooting.md](troubleshooting.md) and [advanced-usage.md](advanced-usage.md).

## Operational best practices

1. **Batch operations** - Use batch upsert/search for efficiency
2. **Payload indexing** - Index fields used in filters
3. **Quantization** - Enable for large collections (>1M vectors)
4. **Sharding** - Use for collections >10M vectors
5. **On-disk storage** - Enable `on_disk_payload` for large payloads
6. **Connection pooling** - Reuse client instances

## Project facts

- Written in Rust: memory-safe, high performance
- Distributed mode via Raft consensus, sharding, replication
- **GitHub**: https://github.com/qdrant/qdrant (22k+ stars)
- **Docs**: https://qdrant.tech/documentation/
- **Python Client**: https://github.com/qdrant/qdrant-client
- **Cloud**: https://cloud.qdrant.io
- **Version**: 1.12.0+
- **License**: Apache 2.0
