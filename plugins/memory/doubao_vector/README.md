# Doubao Vector Memory

Local semantic memory provider for Hermes Agent.

- Embedding model: `doubao-embedding-vision`
- Endpoint: `https://ark.cn-beijing.volces.com/api/coding/v3/embeddings`
- Storage: `$HERMES_HOME/doubao_vector_memory/index.json`
- Activation: `memory.provider: doubao_vector`

It keeps the built-in disk memory enabled and mirrors explicit memory writes plus completed turns into a local vector index. Retrieval is injected through the normal external memory provider `prefetch()` path.
