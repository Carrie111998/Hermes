# Prompt Programs: Context Managers and `@guidance` Functions

How Guidance composes a program — chat role context managers, the `@guidance` decorator for reusable blocks, and stateful functions that interleave generation with Python control flow.

## Context Managers

Guidance uses Pythonic context managers for chat-style interactions.

```python
from guidance import models, system, user, assistant, gen

lm = models.Anthropic("claude-sonnet-4-5-20250929")

# System message
with system():
    lm += "You are a JSON generation expert."

# User message
with user():
    lm += "Generate a person object with name and age."

# Assistant response
with assistant():
    lm += gen("response", max_tokens=100)

print(lm["response"])
```

Benefits:
- Natural chat flow
- Clear role separation
- Easy to read and maintain

The `lm` object is immutable-by-append: `lm += ...` returns a new state, and every `gen`/`select` with a `name` writes a captured variable readable as `lm["name"]`.

## Reusable `@guidance` Functions

Create reusable generation patterns with the `@guidance` decorator.

```python
from guidance import guidance, gen, models

@guidance
def generate_person(lm):
    """Generate a person with name and age."""
    lm += "Name: " + gen("name", max_tokens=20, stop="\n")
    lm += "\nAge: " + gen("age", regex=r"[0-9]+", max_tokens=3)
    return lm

# Use the function
lm = models.Anthropic("claude-sonnet-4-5-20250929")
lm = generate_person(lm)

print(lm["name"])
print(lm["age"])
```

A stateless function (the default) is compiled into a grammar and can be composed inside larger grammars.

## Stateful Functions

`@guidance(stateless=False)` is required whenever the body needs to branch on generated values, call external tools, or loop an unknown number of times.

```python
from guidance import guidance, gen, select

@guidance(stateless=False)
def react_agent(lm, question, tools, max_rounds=5):
    """ReAct agent with tool use."""
    lm += f"Question: {question}\n\n"

    for i in range(max_rounds):
        # Thought
        lm += f"Thought {i+1}: " + gen("thought", stop="\n")

        # Action
        lm += "\nAction: " + select(list(tools.keys()), name="action")

        # Execute tool
        tool_result = tools[lm["action"]]()
        lm += f"\nObservation: {tool_result}\n\n"

        # Check if done
        lm += "Done? " + select(["Yes", "No"], name="done")
        if lm["done"] == "Yes":
            break

    # Final answer
    lm += "\nFinal Answer: " + gen("answer", max_tokens=100)
    return lm
```

Use cases for grammar-based generation inside these programs: complex structured outputs, nested data structures, programming-language syntax, and domain-specific languages. See `constraints.md` for the grammar surface and `examples.md` for full agent and workflow programs.
