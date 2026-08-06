# Skill Measurement & Evaluation

> Loaded when you need to measure skill ROI, set up eval suites, or manage skill discovery at scale.

## Why Measure

Every skill in the index costs ~100 tokens per session per user. Without measurement, you're flying blind:

- **Under-trigger:** skill exists but never loads — wasted effort, or worse, the agent fails silently on tasks it should handle
- **Over-trigger:** skill loads too often — wasted context, degrades other skills' routing
- **Low ROI:** skill loads and runs but doesn't improve outcomes — pure tax
- **Cross-model inconsistency:** skill routes well on one model family but not another

## Eval Suite Types

Perplexity runs three tiers of eval suites for every skill:

### 1. Skill Loading Evals (routing precision/recall)

**Tests:** Does the skill load when it should? Does it NOT load when it shouldn't?

| Metric | What it checks |
|---|---|
| **Precision** | When the skill loads, was it the right call? (false positive rate) |
| **Recall** | When the skill should load, did it? (false negative rate) |
| **Forbidden** | Does it load on queries explicitly in another skill's domain? |

Build positive + negative query sets:
- **Positive:** real user queries where the skill should trigger (sample from production or brain trust)
- **Negative:** queries where the skill must NOT trigger (neighbor domains, generic requests)
- **Neighbor confusion:** queries near the domain boundary — the highest-value negative cases

### 2. Progressive Loading Evals

**Tests:** Once loaded, does the agent actually read the right accessory files?

Example: a finance skill has `references/tax-rates.md`. Eval verifies the agent reads it when asked about tax rates, not just winging it from training data.

### 3. End-to-End Task Evals

**Tests:** Full agent loop — does the skill actually improve task completion?

- Run the agent with the skill enabled vs disabled on the same task set
- Grade with an LLM judge using a domain-specific rubric
- Track the delta: if enabling the skill doesn't improve scores, it's noise

### Cross-Model Testing

Run all eval suites against every supported model family. Perplexity tests against GPT, Claude Opus, and Claude Sonnet simultaneously — routing behavior differs significantly across models. A description that triggers correctly on Claude may not on GPT.

## Usage Telemetry

Anthropic logs skill usage via a PreToolUse hook. This lets them answer:

- **Which skills are popular?** — guides investment in improvement
- **Which skills under-trigger?** — description needs rework, or the skill should be merged/removed
- **Which skills over-trigger?** — description too broad, stealing routing from neighbors

Implementation pattern:
```
# PreToolUse hook → log {skill_name, timestamp, session_id, query_summary}
# Aggregate weekly: load_count, trigger_rate vs expected, false_positive_flags
```

## Skill Lifecycle Management

### Organic adoption (Anthropic model)

1. **Sandbox phase:** author uploads skill to a shared sandbox folder, points people to it via Slack/forum
2. **Traction phase:** if others find it useful, it gains organic adoption
3. **Marketplace phase:** once it has traction, author submits PR to move it into the official marketplace

No central team decides what goes in — adoption is the filter.

### Dependency management

Skills can reference each other by name. The model invokes referenced skills if installed. Example: a `csv-generation` skill depends on a `file-upload` skill — the model chains them automatically.

No native dependency resolution exists yet — reference by name and trust the model to compose.

### Pruning

Skills that consistently under-trigger or show no task-completion delta should be:
1. Have their description revised (eval-backed change)
2. If still no improvement after 2 iterations → merge into a broader skill or remove

Every dead skill in the index is still charging ~100 tokens/session.

## Quick Start: Minimum Viable Eval

If you're not ready for a full eval pipeline, do at least this:

1. Write 3 **positive queries** (should load) and 3 **negative queries** (shouldn't load)
2. Run each query in a fresh session, note whether the skill loads
3. For any false positive/negative → revise description, re-test
4. Repeat until 5/6 correct (perfection is unrealistic, 80-20 is the goal)
