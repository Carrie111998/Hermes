# QA and Repair

## QA Gate

Score each candidate from 0 to 5:

| Field | Question |
|-------|----------|
| `data_as_subject` | Are data, charts, nodes, or process states the visual subject? |
| `metric_fidelity` | Does it only use user-provided facts? |
| `readability` | Are headline numbers and critical labels readable? |
| `semantic_motion` | Does movement express growth, connection, flow, or transformation? |
| `six_panel_structure` | Does the order follow the storyboard? |
| `style_alignment` | Is the selected premium 3D or illustrated tier consistent? |
| `cognitive_load` | Is information density controlled? |
| `final_resolve` | Is the final frame stable and cover-ready? |

Thresholds:

- `metric_fidelity` must be 5.
- `readability`, `semantic_motion`, and `data_as_subject` should be at least 4.

If a candidate fails, repair the plan or prompt and regenerate. Do not explain
away invented metrics, tiny labels, or unstable ending frames.

## Repair Rules

| Failure | Repair |
|---------|--------|
| Numbers too small | Reduce text density and make the main number headline-tier |
| Invented metric appears | Rewrite allowed text and regenerate storyboard/video |
| Chart moves decoratively | Bind motion to a specific semantic change |
| Panel too busy | Split information; one subject per panel |
| Colors too varied | Reduce to two or three core colors |
| Diagram unclear | Establish nodes first, then edges, then data flow |
| Flow unclear | Make each step a separate state |
| Too highMD | Reduce camera motion and strengthen chart build/internal choreography |
| Too cinematic or photoreal | Remove people, office, product hero, and lens language |
| Ending unstable | Rebuild P06 as a stable final resolve |
| Logo or text garbled | Use uploaded logo if supported, simple wordmark, or no logo |

## Verification Checklist

- [ ] Variant is one of `n-stats-sequence`, `process-flow`, or `system-diagram`.
- [ ] Aspect ratio is known.
- [ ] All metrics, steps, nodes, labels, brand text, and tagline are from the
      user.
- [ ] Asset roles are classified: foundation, logo, style reference, or ignore.
- [ ] Foundation exists, either uploaded or selected from a four-up moodboard.
- [ ] Storyboard has exactly six panels in a 3x2 sheet.
- [ ] Each panel has one main information subject.
- [ ] P06 is a stable final resolve.
- [ ] Stage C uses storyboard as the primary reference.
- [ ] QA scores pass the required thresholds before final delivery.
