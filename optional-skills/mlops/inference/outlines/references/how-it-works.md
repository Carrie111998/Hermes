# How Constrained Decoding Works in Outlines

Mechanics of token-level constraint enforcement (schema to CFG to FSM to logit mask) and the resulting speed, memory and accuracy characteristics.

## Constrained Token Sampling

Outlines uses Finite State Machines (FSM) to constrain token generation at the logit level.

How it works:
1. Convert schema (JSON Schema / Pydantic / regex) to a context-free grammar (CFG)
2. Transform the CFG into a Finite State Machine (FSM)
3. Filter invalid tokens at each step during generation
4. Fast-forward when only one valid token exists

Benefits:
- **Zero overhead**: filtering happens at token level
- **Speed improvement**: fast-forward through deterministic paths
- **Guaranteed validity**: invalid outputs are impossible

```python
import outlines
from pydantic import BaseModel

# Pydantic model -> JSON schema -> CFG -> FSM
class Person(BaseModel):
    name: str
    age: int

model = outlines.models.transformers("microsoft/Phi-3-mini-4k-instruct")

# Behind the scenes:
# 1. Person -> JSON schema
# 2. JSON schema -> CFG
# 3. CFG -> FSM
# 4. FSM filters tokens during generation

generator = outlines.generate.json(model, Person)
result = generator("Generate person: Alice, 25")
```

Because the mask is applied inside the sampling loop, the model never emits a token that would break the schema — there is no post-hoc validation step and no retry loop.

## Performance Characteristics

**Speed**
- Zero overhead: structured generation is as fast as unconstrained generation
- Fast-forward optimization skips deterministic tokens
- 1.2-2x faster than post-generation validation approaches

**Memory**
- FSM compiled once per schema (cached)
- Minimal runtime overhead
- Efficient with vLLM for high throughput

**Accuracy**
- 100% valid outputs (guaranteed by the FSM)
- No retry loops needed
- Deterministic token filtering
