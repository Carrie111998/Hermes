---
name: instructor
description: "Instructor: Pydantic-validated structured output from hosted LLM APIs (OpenAI/Anthropic/Gemini tool-calling), with automatic retry on validation failure, streaming partials, and multi-provider patching."
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [instructor, pydantic, openai, anthropic]
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Prompt Engineering, Instructor, Structured Output, Pydantic, Data Extraction, JSON Parsing, Type Safety, Validation, Streaming, OpenAI, Anthropic]

---

# Instructor: Structured LLM Outputs

## When to use / when NOT to use

Use Instructor when you are calling a **hosted LLM API** (Anthropic, OpenAI, Gemini, or any OpenAI-compatible endpoint including Ollama) and you want:

- Structured data extraction validated against a Pydantic model
- Automatic retry that feeds the validation error back to the model
- Streaming partial objects or streamed lists of objects
- One consistent API across providers via client patching

Do NOT use Instructor when:

- You need the constraint enforced **in the logits** so malformed output is impossible — that requires local weights and token-level masking. Use `outlines` (JSON Schema / regex / CFG over transformers, vLLM, llama.cpp) or `guidance` (CFG + multi-step prompt programs, token healing).
- You need prompt optimization / compiled programs (DSPy) or long tool-using chains (LangChain).
- The task is a one-off, throwaway extraction where plain JSON parsing is cheaper.

Note that Instructor validates **after** generation and retries; it cannot guarantee first-pass validity.

## Routing table

| To do this | Read |
|---|---|
| Design the `response_model` (nesting, Optional, enums, unions, `create_model`, field descriptions) | `references/response-models.md` |
| Write validators — numeric/string/date constraints, `field_validator`, `model_validator`, cross-field and business rules | `references/validation.md` |
| Understand the retry loop, catch `ValidationError`, stream partials or iterables | `references/retry-and-streaming.md` |
| Install extras, patch a provider (Anthropic / OpenAI / Ollama), pick a `Mode`, manage client lifecycle | `references/providers.md` |
| Copy a working task pattern — extraction, classification, multi-entity, analysis, batch, streaming | `references/examples.md` |
| Compare against manual JSON / LangChain / DSPy, find upstream docs | `references/comparison.md` |

## Key constraints and gotchas

- Validation is **post-hoc**: `max_retries` (default 3) burns extra API calls on every failure. Cost scales with how strict your schema is.
- After the retries are exhausted the call raises `ValidationError` — always wrap production calls.
- `Field(description=...)` is not decoration: it lands in the tool schema the model sees, and it is what the model reads on a retry. Vague descriptions cause retry loops.
- Mode matters: `Mode.ANTHROPIC_TOOLS` for Claude, `Mode.TOOLS` for OpenAI, `Mode.JSON` as the fallback for providers without native structured output (required for Ollama).
- Anthropic calls need `max_tokens`; a too-small value truncates the tool call and looks like a validation error.
- Over-strict validators (exact regexes, narrow ranges) fail more often than they help — normalize inside a validator instead of rejecting.
- Streaming uses different entry points: `create_partial` for one growing object, `create_iterable` for a stream of items.

## Minimal end-to-end skeleton

```python
import instructor
from anthropic import Anthropic
from pydantic import BaseModel, Field, ValidationError

class User(BaseModel):
    name: str = Field(description="Full name as written in the text")
    age: int = Field(ge=0, le=120, description="Age in years")
    email: str = Field(description="Email address")

client = instructor.from_anthropic(Anthropic())

try:
    user = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": "John Doe is 30 years old. His email is john@example.com"
        }],
        response_model=User,
        max_retries=3,
    )
    print(user.name, user.age, user.email)
except ValidationError as e:
    print(f"Failed after retries: {e}")
```
