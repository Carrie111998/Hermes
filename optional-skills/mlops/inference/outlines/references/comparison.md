# Outlines vs Alternatives

Feature matrix against Instructor, Guidance and LMQL, selection guidance, and upstream resource links.

## Feature Matrix

| Feature | Outlines | Instructor | Guidance | LMQL |
|---------|----------|------------|----------|------|
| Pydantic Support | Native | Native | No | No |
| JSON Schema | Yes | Yes | Limited | Yes |
| Regex Constraints | Yes | No | Yes | Yes |
| Local Models | Full | Limited | Full | Full |
| API Models | Limited | Full | Full | Full |
| Zero Overhead | Yes | No | Partial | Yes |
| Automatic Retrying | No | Yes | No | No |
| Learning Curve | Low | Low | Low | High |

## When to choose Outlines

- Using local models (Transformers, llama.cpp, vLLM)
- Need maximum inference speed
- Want Pydantic model support
- Require zero-overhead structured generation
- Want to control the token sampling process

## When to choose alternatives

- **Instructor**: need hosted API models with automatic retrying
- **Guidance**: need token healing and multi-step prompt programs with control flow
- **LMQL**: prefer declarative query syntax

## Project Facts

- GitHub stars: 8,000+; maintained by dottxt.ai (formerly .txt)

## Resources

- Documentation: https://outlines-dev.github.io/outlines
- GitHub: https://github.com/outlines-dev/outlines (8k+ stars)
- Discord: https://discord.gg/R9DSu34mGd
- Blog: https://blog.dottxt.co
