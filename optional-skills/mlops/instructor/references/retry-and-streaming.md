# Retry Loop, Error Handling and Streaming

Runtime behavior of Instructor: how the validation-failure retry loop works, how to catch failures that survive retries, and how to stream partial objects or iterables.

## Automatic Retrying

Instructor retries automatically when validation fails, feeding the validation error back to the LLM.

```python
user = client.messages.create(
    model="claude-sonnet-4-5-20250929",
    max_tokens=1024,
    messages=[{
        "role": "user",
        "content": "Extract user from: John, age unknown"
    }],
    response_model=User,
    max_retries=3  # Default is 3
)

# If age can't be extracted, Instructor tells the LLM:
# "Validation error: age - field required"
# LLM tries again with better extraction
```

How it works:
1. LLM generates output
2. Pydantic validates
3. If invalid: error message is sent back to the LLM
4. LLM tries again with error feedback
5. Repeats up to `max_retries`

## Handling Validation Errors

```python
from pydantic import ValidationError

try:
    user = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=1024,
        messages=[...],
        response_model=User,
        max_retries=3
    )
except ValidationError as e:
    print(f"Failed after retries: {e}")
    # Handle gracefully

except Exception as e:
    print(f"API error: {e}")
```

## Custom Error Messages / Schema Examples

Descriptions and `json_schema_extra` examples are what the model sees when a retry happens, so make them actionable.

```python
from pydantic import BaseModel, Field, EmailStr

class ValidatedUser(BaseModel):
    name: str = Field(description="Full name, 2-100 characters")
    age: int = Field(description="Age between 0 and 120", ge=0, le=120)
    email: EmailStr = Field(description="Valid email address")

    class Config:
        json_schema_extra = {
            "examples": [
                {
                    "name": "John Doe",
                    "age": 30,
                    "email": "john@example.com"
                }
            ]
        }
```

## Streaming Partial Objects

```python
from pydantic import BaseModel

class Story(BaseModel):
    title: str
    content: str
    tags: list[str]

# Stream partial updates as the LLM generates
for partial_story in client.messages.create_partial(
    model="claude-sonnet-4-5-20250929",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Write a short sci-fi story"}],
    response_model=Story
):
    print(f"Title: {partial_story.title}")
    print(f"Content so far: {partial_story.content[:100]}...")
    # Update UI in real-time
```

`instructor.Partial` is the type helper behind this API (`from instructor import Partial`) when you need to annotate the partial type explicitly.

## Streaming Iterables

```python
class Task(BaseModel):
    title: str
    priority: str

# Stream list items as they're generated
tasks = client.messages.create_iterable(
    model="claude-sonnet-4-5-20250929",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Generate 10 project tasks"}],
    response_model=Task
)

for task in tasks:
    print(f"- {task.title} ({task.priority})")
    # Process each task as it arrives
```
