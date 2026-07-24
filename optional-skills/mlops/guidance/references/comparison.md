# Guidance vs Alternatives

Feature matrix against Instructor, Outlines and LMQL, selection guidance, performance characteristics, and upstream resource links.

## Feature Matrix

| Feature | Guidance | Instructor | Outlines | LMQL |
|---------|----------|------------|----------|------|
| Regex Constraints | Yes | No | Yes | Yes |
| Grammar Support | CFG | No | CFG | CFG |
| Pydantic Validation | No | Yes | Yes | No |
| Token Healing | Yes | No | Yes | No |
| Local Models | Yes | Limited | Yes | Yes |
| API Models | Yes | Yes | Limited | Yes |
| Pythonic Syntax | Yes | Yes | Yes | No (SQL-like) |
| Learning Curve | Low | Low | Medium | High |

## When to choose Guidance

- Need regex/grammar constraints
- Want token healing
- Building complex workflows with control flow
- Using local models (Transformers, llama.cpp)
- Prefer Pythonic syntax

## When to choose alternatives

- **Instructor**: need Pydantic validation with automatic retrying against hosted APIs
- **Outlines**: need JSON Schema / Pydantic-driven generation
- **LMQL**: prefer declarative query syntax

## Performance Characteristics

**Latency reduction**
- 30-50% faster than traditional prompting for constrained outputs
- Token healing reduces unnecessary regeneration
- Grammar constraints prevent invalid token generation

**Memory usage**
- Minimal overhead vs unconstrained generation
- Grammar compilation cached after first use
- Efficient token filtering at inference time

**Token efficiency**
- Prevents wasted tokens on invalid outputs
- No need for retry loops
- Direct path to valid outputs

## Project Facts

- GitHub stars: 18,000+; originated at Microsoft Research

## Resources

- Documentation: https://guidance.readthedocs.io
- GitHub: https://github.com/guidance-ai/guidance (18k+ stars)
- Notebooks: https://github.com/guidance-ai/guidance/tree/main/notebooks
- Discord: community support available
