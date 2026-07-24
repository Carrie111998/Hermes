# Variants and Input Contract

Choose exactly one primary structure. Dashboard is a composition form, not a
fourth variant.

## Input Contract

Infer what is unambiguous, but never invent facts.

Required by variant:

| Variant | Required user-provided information |
|---------|------------------------------------|
| `n-stats-sequence` | Exact metric strings, such as `ARR +34%`, `84% retention`, `12K active users` |
| `process-flow` | Exact steps, such as `Upload -> Clean -> Analyze -> Export` |
| `system-diagram` | Exact nodes and relationships, such as `App -> API -> Model -> Database -> Dashboard` |

Ask only for missing material that changes the workflow:

- aspect ratio: `16:9`, `9:16`, `1:1`, `4:3`, or `3:4`
- variant: `n-stats-sequence`, `process-flow`, or `system-diagram`
- exact metrics, steps, nodes, or relationships
- which image is foundation, logo, style reference, or ignored when multiple
  images are supplied
- logo upload or skip when the user mentions a logo but does not provide one
- moodboard frame choice when no foundation image exists

Do not ask whether to create the storyboard, which provider/model to use, or
whether to generate multiple candidates. Those are workflow defaults unless the
user explicitly overrides them.

## n-stats-sequence

Use for two or more metrics, KPI sequences, growth stats, investor highlights,
dashboard numbers, market-report data, and annual-report data.

If the user gives only one metric, this variant can still be used, but do not
invent supporting comparisons. Decompose only facts the user supplied.

Typical sequence:

1. stat or hook metric
2. second metric
3. third metric or supplied context
4. dashboard, comparison, or aggregate reveal
5. synthesis or insight using only supplied text
6. wordmark, final insight, or stable brand resolve

## process-flow

Use for steps, workflow, pipeline, state transition, and how-it-works requests.

Typical sequence:

1. step 1
2. step 2
3. step 3
4. step 4 or transformation if supplied
5. resolved system state
6. wordmark or final resolve

## system-diagram

Use for architecture, system maps, entities, modules, relationships, networks,
and data flow.

Typical sequence:

1. entry node
2. second module appears
3. connections form
4. data moves through edges
5. full system reveal
6. wordmark or final resolve
