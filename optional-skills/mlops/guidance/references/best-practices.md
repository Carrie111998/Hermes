# Guidance Best Practices

Do/don't pairs for choosing between `gen`, `regex` and `select`, using stop sequences, factoring reusable functions, and keeping constraints loose enough to succeed.

## 1. Use Regex for Format Validation

```python
# ✅ Good: regex ensures valid format
lm += "Email: " + gen("email", regex=r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

# ❌ Bad: free generation may produce invalid emails
lm += "Email: " + gen("email", max_tokens=50)
```

## 2. Use `select()` for Fixed Categories

```python
# ✅ Good: guaranteed valid category
lm += "Status: " + select(["pending", "approved", "rejected"], name="status")

# ❌ Bad: may generate typos or invalid values
lm += "Status: " + gen("status", max_tokens=20)
```

## 3. Leverage Token Healing

```python
# Token healing is enabled by default
# No special action needed - just concatenate naturally
lm += "The capital is " + gen("capital")  # Automatic healing
```

## 4. Use `stop` Sequences

```python
# ✅ Good: stop at newline for single-line outputs
lm += "Name: " + gen("name", stop="\n")

# ❌ Bad: may generate multiple lines
lm += "Name: " + gen("name", max_tokens=50)
```

## 5. Create Reusable Functions

```python
# ✅ Good: reusable pattern
@guidance
def generate_person(lm):
    lm += "Name: " + gen("name", stop="\n")
    lm += "\nAge: " + gen("age", regex=r"[0-9]+")
    return lm

# Use multiple times
lm = generate_person(lm)
lm += "\n\n"
lm = generate_person(lm)
```

## 6. Balance Constraints

```python
# ✅ Good: reasonable constraints
lm += gen("name", regex=r"[A-Za-z ]+", max_tokens=30)

# ❌ Too strict: may fail or be very slow
lm += gen("name", regex=r"^(John|Jane)$", max_tokens=10)
```
