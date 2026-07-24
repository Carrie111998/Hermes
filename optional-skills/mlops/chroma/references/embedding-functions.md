# Chroma Embedding Functions

Which embedding function to attach to a collection: the built-in default, hosted providers, and writing your own.

## Default (Sentence Transformers)

```python
# Uses sentence-transformers by default
collection = client.create_collection("my_docs")
# Default model: all-MiniLM-L6-v2
```

Requires the `sentence-transformers` package; runs locally on CPU.

## OpenAI

```python
from chromadb.utils import embedding_functions

openai_ef = embedding_functions.OpenAIEmbeddingFunction(
    api_key="your-key",
    model_name="text-embedding-3-small"
)

collection = client.create_collection(
    name="openai_docs",
    embedding_function=openai_ef
)
```

## HuggingFace

```python
huggingface_ef = embedding_functions.HuggingFaceEmbeddingFunction(
    api_key="your-key",
    model_name="sentence-transformers/all-mpnet-base-v2"
)

collection = client.create_collection(
    name="hf_docs",
    embedding_function=huggingface_ef
)
```

## Custom embedding function

```python
from chromadb import Documents, EmbeddingFunction, Embeddings

class MyEmbeddingFunction(EmbeddingFunction):
    def __call__(self, input: Documents) -> Embeddings:
        # Your embedding logic
        return embeddings

my_ef = MyEmbeddingFunction()
collection = client.create_collection(
    name="custom_docs",
    embedding_function=my_ef
)
```

## Rules

- The embedding function is bound to the collection at creation time. Reading a
  collection later with a different function produces meaningless distances — always
  pass the same `embedding_function` to `get_collection`.
- Skip the embedding function entirely if you supply `embeddings=` yourself in `add`
  and `query_embeddings=` in `query`.
