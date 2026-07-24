---
name: dspy
description: "DSPy: declarative LM programs, auto-optimize prompts, RAG."
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [dspy, openai, anthropic]
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Prompt Engineering, DSPy, Declarative Programming, RAG, Agents, Prompt Optimization, LM Programming, Stanford NLP, Automatic Optimization, Modular AI]

---

# DSPy: Declarative Language Model Programming

Program LMs with signatures and modules, then let optimizers write the prompts
from data. Created by Stanford NLP (22k+ GitHub stars).

## When to use this skill

**Use DSPy when:**
- Building complex AI systems with multiple components and workflows
- Programming LMs declaratively instead of hand-tuning prompt strings
- Optimizing prompts automatically from labeled or bootstrapped data
- Creating modular, portable AI pipelines (RAG, agents, classifiers)
- Improving output quality systematically rather than by trial and error

**Do NOT use DSPy when:**
- You need a one-off prompt or a quick prototype — plain API calls are faster
- You have no training data and no way to generate any (optimizers are the point)
- You only need to glue existing tools into a fixed chain (LangChain fits better)
- Your optimization logic is bespoke and must be fully hand-controlled

## Routing table

| To do this | Read |
|---|---|
| Install DSPy, configure Claude/OpenAI/Ollama, route cheap vs strong models, enable tracing | `references/setup-and-providers.md` |
| Pick or compose a module: signatures, `Predict`, `ChainOfThought`, `ProgramOfThought`, `ReAct`, `MultiChainComparison`, `majority`, `TypedPredictor`, `Retry`, `Assert`, batching, save/load | `references/modules.md` |
| Choose and run an optimizer: `BootstrapFewShot`, `MIPRO`, `BootstrapFinetune`, `COPRO`, `KNNFewShot`; write metrics; evaluate and compare | `references/optimizers.md` |
| Copy a working system: RAG (basic/optimized/multi-hop/reranked), ReAct and multi-agent, classifiers, extraction, batch processing, production tips, support bot | `references/examples.md` |
| Iteration order, signature style, data selection, persistence, debugging; DSPy vs manual prompting vs LangChain | `references/best-practices.md` |

## Key constraints and gotchas

- Configure an LM on `dspy.settings` before calling any module, or every call fails.
- The signature docstring and field `desc` **are** the prompt. Vague signatures
  leave the optimizer nothing to improve.
- Optimization output lives in the saved JSON (instructions + demos), not in your
  code — always `save()` a compiled module, otherwise you pay to recompile.
- Never optimize on the test set. Split train/val/test; MIPRO needs a real `valset`.
- Metric must match the task: a binary metric on a nuanced task gives the optimizer
  a flat signal and it will not improve.
- More demos is not better — too many `max_bootstrapped_demos` overfits the trainset.
- MIPRO wants 50-200 examples and 10-30 minutes; BootstrapFinetune wants 100+ and
  an external fine-tuning step. Start with BootstrapFewShot.
- `ReAct` degrades past ~5-7 tools; keep tool docstrings specific.

## End-to-end skeleton

```python
import dspy
from dspy.teleprompt import BootstrapFewShot

# 1. Configure the LM
dspy.settings.configure(lm=dspy.Claude(model="claude-sonnet-4-5-20250929"))

# 2. Declare the task
class QA(dspy.Signature):
    """Answer questions with short factual answers."""
    question = dspy.InputField()
    answer = dspy.OutputField(desc="often between 1 and 5 words")

# 3. Pick a module
qa = dspy.ChainOfThought(QA)

# 4. Data + metric
trainset = [
    dspy.Example(question="What is 2+2?", answer="4").with_inputs("question"),
    dspy.Example(question="What is 3+5?", answer="8").with_inputs("question"),
]

def validate_answer(example, pred, trace=None):
    return example.answer == pred.answer

# 5. Optimize, evaluate, persist
optimized_qa = BootstrapFewShot(
    metric=validate_answer, max_bootstrapped_demos=3
).compile(qa, trainset=trainset)

print(optimized_qa(question="What is the capital of France?").answer)
optimized_qa.save("models/qa_v1.json")
```

## Resources

- **Documentation**: https://dspy.ai
- **GitHub**: https://github.com/stanfordnlp/dspy (22k+ stars)
- **Discord**: https://discord.gg/XCGy2WDCQB
- **Twitter**: @DSPyOSS
- **Paper**: "DSPy: Compiling Declarative Language Model Calls into Self-Improving Pipelines"
