# DSPy Setup and LM Providers

Installation options and how to configure/switch language-model backends (Claude, OpenAI, local Ollama, multi-model routing).

## Installation

```bash
# Stable release
pip install dspy

# Latest development version
pip install git+https://github.com/stanfordnlp/dspy.git

# With specific LM providers
pip install dspy[openai]        # OpenAI
pip install dspy[anthropic]     # Anthropic Claude
pip install dspy[all]           # All providers
```

## Global configuration

Every DSPy program needs an LM configured on `dspy.settings` before any module runs.

```python
import dspy

lm = dspy.Claude(model="claude-sonnet-4-5-20250929")
dspy.settings.configure(lm=lm)
```

## Anthropic Claude

```python
import dspy

lm = dspy.Claude(
    model="claude-sonnet-4-5-20250929",
    api_key="your-api-key",  # Or set ANTHROPIC_API_KEY env var
    max_tokens=1000,
    temperature=0.7
)
dspy.settings.configure(lm=lm)
```

## OpenAI

```python
lm = dspy.OpenAI(
    model="<model>",
    api_key="your-api-key",
    max_tokens=1000
)
dspy.settings.configure(lm=lm)
```

## Local models (Ollama)

```python
lm = dspy.OllamaLocal(
    model="llama3.1",
    base_url="http://localhost:11434"
)
dspy.settings.configure(lm=lm)
```

## Multiple models in one program

Use a cheap model for mechanical steps (retrieval, query rewriting) and a strong
model for reasoning. `dspy.settings.context` scopes the LM to a block.

```python
# Different models for different tasks
cheap_lm = dspy.OpenAI(model="<cheap-model>")
strong_lm = dspy.Claude(model="<strong-model>")

# Use cheap model for retrieval, strong model for reasoning
with dspy.settings.context(lm=cheap_lm):
    context = retriever(question)

with dspy.settings.context(lm=strong_lm):
    answer = generator(context=context, question=question)
```

## Retriever configuration

Retrievers are configured the same way, via `rm=`:

```python
from dspy.retrieve.chromadb_rm import ChromadbRM

retriever = ChromadbRM(
    collection_name="documents",
    persist_directory="./chroma_db",
    k=3
)
dspy.settings.configure(rm=retriever)
```

## Tracing

```python
# Enable tracing
dspy.settings.configure(lm=lm, trace=[])

result = qa(question="...")

# Inspect trace
for call in dspy.settings.trace:
    print(f"Prompt: {call['prompt']}")
    print(f"Response: {call['response']}")
```
