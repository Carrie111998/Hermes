---
name: guidance
description: "Guidance: CFG grammar + multi-step prompt programs over local weights - interleave generation with control flow, token healing, regex/grammar constraints, Microsoft Research."
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [guidance, transformers]
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Prompt Engineering, Guidance, Constrained Generation, Structured Output, JSON Validation, Grammar, Microsoft Research, Format Enforcement, Multi-Step Workflows]

---

# Guidance: Constrained LLM Generation

## When to use / when NOT to use

Use Guidance when the program itself is the unit of work — generation interleaved with Python control flow over local weights:

- Multi-step prompt programs: loops, branching on generated values, tool calls between generations (ReAct-style agents)
- CFG grammars for output that is not naturally a JSON object (DSLs, code, templated text)
- Regex-constrained fields (dates, emails, IDs) and `select()` over a closed set
- Token healing across the prompt/generation boundary, so partial words and camelCase joins do not break
- Reusable, composable generation blocks via `@guidance`

Do NOT use Guidance when:

- You just want a Pydantic object out of a **hosted API** call with retry-on-invalid — use `instructor`.
- You just want JSON Schema / Pydantic-driven generation from local weights with nothing else around it — use `outlines`, which has native Pydantic support (Guidance has none).
- You want a declarative query language rather than Python — that is LMQL.

## Routing table

| To do this | Read |
|---|---|
| Compose a program — chat role context managers, `@guidance` blocks, `stateless=False` for control flow and tool calls | `references/prompt-programs.md` |
| Write constraints — regex cookbook (numbers, dates, emails, IDs, JSON fields), CFG grammars, `select()`, token healing details, grammar caching | `references/constraints.md` |
| Configure a backend — Anthropic/OpenAI keys and model matrix, Transformers device/quantization, llama.cpp GGUF quant levels, tuning | `references/backends.md` |
| Copy a production program — JSON generation, extraction, classification, agents, multi-step workflows, code generation, caching/monitoring | `references/examples.md` |
| Choose between `gen`/`regex`/`select`, stop sequences, avoiding over-strict constraints | `references/best-practices.md` |
| Compare against Instructor / Outlines / LMQL, see latency and token-efficiency figures | `references/comparison.md` |

## Key constraints and gotchas

- **No Pydantic validation.** Guidance constrains tokens; it never hands you a validated typed object. Build the structure yourself with regex/grammar, or use Outlines/Instructor.
- Token healing is on by default and is why you should concatenate prompt fragments naturally rather than pre-trimming trailing spaces.
- Every value you want to read back needs a `name`: `gen("x", ...)` then `lm["x"]`. Unnamed generations are only visible in the rendered text.
- `@guidance` is stateless by default; branching on a generated value or calling a tool mid-program requires `@guidance(stateless=False)`.
- Over-strict regexes (alternations over exact literals) either fail or become very slow; grammar compilation is cached only after first use.
- Always pair `gen` with `stop` or a tight `max_tokens` — otherwise single-line fields run on.
- Constraint enforcement is strongest on local backends; API backends are supported but the grammar surface is narrower.

## Minimal end-to-end skeleton

```python
from guidance import models, gen, select, system, user, assistant

# Local weights (or models.Anthropic(...) / models.OpenAI("<model>"))
lm = models.Transformers("microsoft/Phi-4-mini-instruct", device="cuda")

with system():
    lm += "You extract structured facts."

with user():
    lm += "Tim Cook announced at Apple Park on 2024-09-15 in Cupertino."

with assistant():
    lm += "Person: " + gen("person", stop="\n", max_tokens=30)
    lm += "\nDate: " + gen("date", regex=r"\d{4}-\d{2}-\d{2}", max_tokens=10)
    lm += "\nSentiment: " + select(["positive", "negative", "neutral"], name="sentiment")

print(lm["person"], lm["date"], lm["sentiment"])
```
