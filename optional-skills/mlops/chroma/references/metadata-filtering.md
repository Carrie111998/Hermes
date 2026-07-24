# Chroma Metadata Filtering

The `where` filter DSL used by `query`, `get` and `delete`: exact match, comparison, logical and membership operators.

## Exact match

```python
results = collection.query(
    query_texts=["query"],
    where={"category": "tutorial"}
)
```

## Comparison operators

```python
results = collection.query(
    query_texts=["query"],
    where={"page": {"$gt": 10}}  # $gt, $gte, $lt, $lte, $ne
)
```

## Logical operators

```python
results = collection.query(
    query_texts=["query"],
    where={
        "$and": [
            {"category": "tutorial"},
            {"difficulty": {"$lte": 3}}
        ]
    }  # Also: $or
)
```

## Membership (`$in`)

```python
results = collection.query(
    query_texts=["query"],
    where={"tags": {"$in": ["python", "ml"]}}
)
```

## Notes

- The same `where` dict works on `collection.get(where=..., limit=...)` and
  `collection.delete(where=...)`.
- Metadata values must be scalars (str / int / float / bool); nested objects are not
  filterable.
- Chroma also exposes `where_document={"$contains": "text"}` for full-text substring
  matching over the stored document body.
