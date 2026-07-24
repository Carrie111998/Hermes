# Generator API Reference

The `outlines.generate.*` constructors — choice, json, regex, integer, float — and what each one guarantees about the output.

A generator is built once from a model plus a constraint, then called with prompts. Reuse the generator across prompts: the FSM is compiled at construction time.

## Choice Generator

Restricts the output to one of a fixed list of strings.

```python
generator = outlines.generate.choice(
    model,
    ["positive", "negative", "neutral"]
)

sentiment = generator("Review: This is great!")
# Result: exactly one of the three choices
```

## JSON Generator

Takes a Pydantic model (or a JSON Schema dict) and returns a parsed instance.

```python
from pydantic import BaseModel

class Product(BaseModel):
    name: str
    price: float
    in_stock: bool

generator = outlines.generate.json(model, Product)
product = generator("Extract: iPhone 15, $999, available")

print(type(product))  # <class '__main__.Product'>
```

See `json_generation.md` for the full schema/typing surface.

## Regex Generator

```python
generator = outlines.generate.regex(
    model,
    r"[0-9]{3}-[0-9]{3}-[0-9]{4}"  # Phone number pattern
)

phone = generator("Generate phone number:")
# Result: "555-123-4567" (guaranteed to match the pattern)
```

## Integer and Float Generators

```python
int_generator = outlines.generate.integer(model)
age = int_generator("Person's age:")  # Guaranteed integer

float_generator = outlines.generate.float(model)
price = float_generator("Product price:")  # Guaranteed float
```

## Choosing Between Them

| Need | Generator |
|---|---|
| One label from a closed set | `generate.choice` |
| A typed object / nested structure | `generate.json` |
| A string matching an exact format (phone, SKU, date) | `generate.regex` |
| A bare number | `generate.integer` / `generate.float` |
