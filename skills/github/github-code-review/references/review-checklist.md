# Code Review Checklist

When performing a code review (local or PR), systematically check each category.

## Correctness

- Does the code do what it claims?
- Edge cases handled (empty inputs, nulls, large data, concurrent access)?
- Error paths handled gracefully?

## Security

- No hardcoded secrets, credentials, or API keys
- Input validation on user-facing inputs
- No SQL injection, XSS, or path traversal
- Auth/authz checks where needed

## Code Quality

- Clear naming (variables, functions, classes)
- No unnecessary complexity or premature abstraction
- DRY — no duplicated logic that should be extracted
- Functions are focused (single responsibility)

## Testing

- New code paths tested?
- Happy path and error cases covered?
- Tests readable and maintainable?

## Performance

- No N+1 queries or unnecessary loops
- Appropriate caching where beneficial
- No blocking operations in async code paths

## Documentation

- Public APIs documented
- Non-obvious logic has comments explaining "why"
- README updated if behavior changed

## Running Automated Checks Locally

After checking out the PR branch, let the tooling find the mechanical problems
before you read for design:

```bash
# Run tests if there's a test suite
python -m pytest 2>&1 | tail -20
# or: npm test, cargo test, go test ./..., etc.

# Run linter if configured
ruff check . 2>&1 | head -30
# or: eslint, clippy, etc.
```

## Verdict Mapping

- **Approve** — no critical or warning-level issues, only minor suggestions or all
  clear
- **Request Changes** — any critical or warning-level issue that should be fixed
  before merge
- **Comment** — observations and suggestions, but nothing blocking (use when you're
  unsure or the PR is a draft)
