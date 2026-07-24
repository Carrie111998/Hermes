# DSPy Best Practices and Framework Comparison

Working habits for DSPy programs (iteration order, signature style, data, persistence, debugging) plus how DSPy compares to manual prompting and LangChain.

## 1. Start simple, iterate

```python
# Start with Predict
qa = dspy.Predict("question -> answer")

# Add reasoning if needed
qa = dspy.ChainOfThought("question -> answer")

# Add optimization when you have data
optimized_qa = optimizer.compile(qa, trainset=data)
```

## 2. Use descriptive signatures

The docstring and field `desc` are the actual prompt material — vague signatures
give the optimizer nothing to work with.

```python
# ❌ Bad: Vague
class Task(dspy.Signature):
    input = dspy.InputField()
    output = dspy.OutputField()

# ✅ Good: Descriptive
class SummarizeArticle(dspy.Signature):
    """Summarize news articles into 3-5 key points."""
    article = dspy.InputField(desc="full article text")
    summary = dspy.OutputField(desc="bullet points, 3-5 items")
```

## 3. Optimize with representative data

```python
# Create diverse training examples
trainset = [
    dspy.Example(question="factual", answer="...").with_inputs("question"),
    dspy.Example(question="reasoning", answer="...").with_inputs("question"),
    dspy.Example(question="calculation", answer="...").with_inputs("question"),
]

# Use validation set for metric
def metric(example, pred, trace=None):
    return example.answer in pred.answer
```

## 4. Save and load optimized models

Optimization results live in the saved JSON (instructions + demos), not in code —
losing it means re-running the optimizer.

```python
# Save
optimized_qa.save("models/qa_v1.json")

# Load
loaded_qa = dspy.ChainOfThought("question -> answer")
loaded_qa.load("models/qa_v1.json")
```

## 5. Monitor and debug

```python
# Enable tracing
dspy.settings.configure(lm=lm, trace=[])

# Run prediction
result = qa(question="...")

# Inspect trace
for call in dspy.settings.trace:
    print(f"Prompt: {call['prompt']}")
    print(f"Response: {call['response']}")
```

## Comparison to other approaches

| Feature | Manual Prompting | LangChain | DSPy |
|---------|-----------------|-----------|------|
| Prompt Engineering | Manual | Manual | Automatic |
| Optimization | Trial & error | None | Data-driven |
| Modularity | Low | Medium | High |
| Type Safety | No | Limited | Yes (Signatures) |
| Portability | Low | Medium | High |
| Learning Curve | Low | Medium | Medium-High |

**When to choose DSPy:**
- You have training data or can generate it
- You need systematic prompt improvement
- You're building complex multi-stage systems
- You want to optimize across different LMs

**When to choose alternatives:**
- Quick prototypes (manual prompting)
- Simple chains with existing tools (LangChain)
- Custom optimization logic needed
