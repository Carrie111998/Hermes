# Response Model Design

How to shape the Pydantic `response_model` you hand to Instructor: field typing, nesting, optionality, enums, union types, runtime-built models, and schema-design best practices.

## Basic Model

Response models define the structure and validation rules for LLM outputs.

```python
from pydantic import BaseModel, Field

class Article(BaseModel):
    title: str = Field(description="Article title")
    author: str = Field(description="Author name")
    word_count: int = Field(description="Number of words", gt=0)
    tags: list[str] = Field(description="List of relevant tags")

article = client.messages.create(
    model="claude-sonnet-4-5-20250929",
    max_tokens=1024,
    messages=[{
        "role": "user",
        "content": "Analyze this article: [article text]"
    }],
    response_model=Article
)
```

Benefits:
- Type safety with Python type hints
- Automatic validation (`word_count > 0`)
- Self-documenting with `Field` descriptions
- IDE autocomplete support

## Nested Models

```python
class Address(BaseModel):
    street: str
    city: str
    country: str

class Person(BaseModel):
    name: str
    age: int
    address: Address  # Nested model

person = client.messages.create(
    model="claude-sonnet-4-5-20250929",
    max_tokens=1024,
    messages=[{
        "role": "user",
        "content": "John lives at 123 Main St, Boston, USA"
    }],
    response_model=Person
)

print(person.address.city)  # "Boston"
```

## Optional Fields and Defaults

```python
from typing import Optional

class Product(BaseModel):
    name: str
    price: float
    discount: Optional[float] = None  # Optional
    description: str = Field(default="No description")  # Default value

# LLM doesn't need to provide discount or description
```

Graceful-degradation shape for partial data:

```python
class PartialData(BaseModel):
    required_field: str
    optional_field: Optional[str] = None
    default_field: str = "default_value"

# LLM only needs to provide required_field
```

## Enums for Constraints

```python
from enum import Enum

class Sentiment(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"

class Review(BaseModel):
    text: str
    sentiment: Sentiment  # Only these 3 values allowed

review = client.messages.create(
    model="claude-sonnet-4-5-20250929",
    max_tokens=1024,
    messages=[{"role": "user", "content": "This product is amazing!"}],
    response_model=Review
)

print(review.sentiment)  # Sentiment.POSITIVE
```

Use enums whenever the value set is fixed:

```python
class Status(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

class Application(BaseModel):
    status: Status  # LLM must choose from enum
```

## Union Types

```python
from typing import Union
from pydantic import HttpUrl

class TextContent(BaseModel):
    type: str = "text"
    content: str

class ImageContent(BaseModel):
    type: str = "image"
    url: HttpUrl
    caption: str

class Post(BaseModel):
    title: str
    content: Union[TextContent, ImageContent]  # Either type

# LLM chooses appropriate type based on content
```

## Dynamic Models

```python
from pydantic import create_model, Field, EmailStr

# Create model at runtime
DynamicUser = create_model(
    'User',
    name=(str, ...),
    age=(int, Field(ge=0)),
    email=(EmailStr, ...)
)

user = client.messages.create(
    model="claude-sonnet-4-5-20250929",
    max_tokens=1024,
    messages=[...],
    response_model=DynamicUser
)
```

## Best Practices

### 1. Clear field descriptions

```python
# Bad: vague
class Product(BaseModel):
    name: str
    price: float

# Good: descriptive
class Product(BaseModel):
    name: str = Field(description="Product name from the text")
    price: float = Field(description="Price in USD, without currency symbol")
```

### 2. Use appropriate validation

```python
class Rating(BaseModel):
    score: int = Field(ge=1, le=5, description="Rating from 1 to 5 stars")
    review: str = Field(min_length=10, description="Review text, at least 10 chars")
```

### 3. Provide examples in prompts

```python
messages = [{
    "role": "user",
    "content": """Extract person info from: "John, 30, engineer"

Example format:
{
  "name": "John Doe",
  "age": 30,
  "occupation": "engineer"
}"""
}]
```

### 4. Use enums for fixed categories, Optional/defaults for data that may be missing

See the Enums and Optional Fields sections above.
