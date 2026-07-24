---
name: outlines
description: "Outlines: token-level constrained decoding for local models (transformers/vLLM/llama.cpp) - JSON Schema, regex and CFG grammars enforced in the logits, so output cannot be malformed."
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [outlines, transformers, vllm, pydantic]
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Prompt Engineering, Outlines, Structured Generation, JSON Schema, Pydantic, Local Models, Grammar-Based Generation, vLLM, Transformers, Type Safety]

---

# Outlines: Structured Text Generation

## When to use / when NOT to use

Use Outlines when you hold the **weights locally** (Transformers, llama.cpp GGUF, vLLM) and want the constraint enforced inside the sampling loop:

- Guaranteed-valid JSON matching a Pydantic model or raw JSON Schema
- Output forced to match a regex (dates, phone numbers, SKUs, IDs)
- A label picked from a closed set, or a bare integer/float
- Maximum throughput — masking is zero-overhead, no retries, no wasted tokens

Do NOT use Outlines when:

- You are calling a **hosted API** (Anthropic, OpenAI, Gemini). API support exists but is limited and loses the logit-level guarantee; use `instructor` instead, which validates with Pydantic and retries on failure.
- You need interleaved control flow across a multi-step prompt program, token healing, or agent loops that alternate generation and Python — use `guidance`.
- You want automatic repair of a bad answer: Outlines has no retry mechanism, because malformed output cannot occur in the first place. Semantically *wrong* but well-formed output is still possible.

## Routing table

| To do this | Read |
|---|---|
| Pick the right generator (`choice` / `json` / `regex` / `integer` / `float`) and see its guarantees | `references/generators.md` |
| Build the schema — Pydantic models, field constraints, enums/Literals, nesting, unions, raw JSON Schema, lists of objects, caching | `references/json_generation.md` |
| Load and tune a backend — Transformers device/dtype/quantization, llama.cpp GGUF quant levels, vLLM tensor parallelism, OpenAI limits, Docker/env deployment | `references/backends.md` |
| Understand FSM/CFG masking internals and speed/memory/accuracy characteristics | `references/how-it-works.md` |
| Copy a production example — extraction, classification, forms, multi-entity, code generation, batch/CSV pipelines | `references/examples.md` |
| Compare against Instructor / Guidance / LMQL, find upstream docs | `references/comparison.md` |

## Key constraints and gotchas

- The schema constrains **shape only**. A model can still emit structurally valid nonsense; the prompt must supply the task context.
- Generator construction compiles the FSM. Build once, reuse across prompts — rebuilding per call is the most common performance mistake.
- Deeply nested schemas compile slower and generate slower than flat ones; prefer flat structures on hot paths.
- No retry loop exists. Validation errors surface as Pydantic errors only if you re-validate manually; mark uncertain fields `Optional` instead of forcing the model to invent values.
- API backends (OpenAI) support only a subset of constraints — regex and CFG are local-only in practice.
- Regexes that are too permissive (`.*`) defeat the purpose; regexes that are too narrow make generation fail or crawl.
- Use specific types (`float`, `int`, `bool`) not `str`/`Any` — the type is what produces the token mask.

## Minimal end-to-end skeleton

```python
import outlines
from pydantic import BaseModel, Field

class User(BaseModel):
    name: str = Field(description="Full name")
    age: int = Field(ge=0, le=120)
    email: str

# 1. Load local weights once
model = outlines.models.transformers("microsoft/Phi-3-mini-4k-instruct")

# 2. Compile the constraint once
generator = outlines.generate.json(model, User)

# 3. Call it with as many prompts as you like
user = generator("Extract user: John Doe, 30 years old, john@example.com")

print(user.name, user.age, user.email)  # guaranteed to parse as User
```
