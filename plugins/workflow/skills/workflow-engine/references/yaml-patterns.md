# Workflow YAML Patterns

## Linear Pipeline (no loops)

```yaml
name: feature-dev
description: "Execution pipeline from task cards to merge"
trigger_events: ["workflow_dispatch"]
kanban_board: adventours

nodes:
  coder-build:
    agent: "{coder}"
    task: "Implement the feature"
    depends_on: []

  ci-check:
    agent: "{sherlock}"
    task: "Run CI checks"
    depends_on: [coder-build]

  review:
    agent: "{raven}"
    task: "Code review"
    depends_on: [ci-check]

  merge:
    agent: "{sherlock}"
    task: "Merge to main"
    depends_on: [review]
```

## Loop Pipeline (with revision cycle)

```yaml
name: implementation
description: "Spec-to-merge with QA loop"
trigger_events: ["workflow_dispatch"]

nodes:
  coder-implement:
    agent: "{coder}"
    task: "Implement the feature"
    depends_on: []

  qa-verify:
    agent: "{qa}"
    task: "Run tests and verify"
    depends_on: [coder-implement]

  coder-revise:
    agent: "{coder}"
    task: "Fix issues found by QA"
    depends_on: [qa-verify]  # Creates loop zone
```

**Loop detection:** Nodes with "revise" in the name are revision nodes. Their dependencies are verify nodes. The engine detects this pattern and uses kanban hooks for the loop.

## File Delivery via Attachments

Instead of `{inputs.file_path}` template variables (which may not resolve in looped workflows), use attachments:

```yaml
nodes:
  enrich-artifact:
    agent: "{analyst}"
    task: "Read the grill artifact attached to this card. Identify technologies..."
    depends_on: []
```

Call with: `workflow_start(..., attachments=["/path/to/file.md"])`

The file is attached to the first-layer card. The agent reads it via `list_attachments`.

## Template Variables

**Bare form:** `{question}` resolves from `context["question"]`
**Namespaced form:** `{inputs.key}` resolves from `context["inputs"]["key"]`
**Upstream output:** `{node-id}` or `{phaseN.node-id}` resolves from `states[node-id].result`

## Agent Assignment

Agents are specified as `agent: "{profile-name}"` in YAML. The curly braces are required for template substitution. Common agents:
- `{coder}` — implementation
- `{qa}` — testing and verification
- `{analyst}` — research and enrichment
- `{spec-author}` — specification writing
- `{security}` — security review
- `{sherlock}` — CI/CD and code review
- `{raven}` — code review
