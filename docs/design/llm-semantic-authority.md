# LLM Semantic Authority

Status: binding architecture contract.

## Rule

The LLM is the only component allowed to interpret meaning.

Runtime code must not infer intent, risk, business meaning, ownership,
priority, route, approval need, or next action from free-form text. This
includes user messages, model prose, tool questions and choices, titles,
descriptions, shell commands, code, and generated content.

The following are prohibited as semantic authority:

- keyword or phrase allowlists and denylists;
- regular-expression grammars over free-form text;
- cue lists, scores, or hand-written intent/risk classifiers;
- semantic routers, dispatchers, fallback trees, and policy engines;
- post-model overrides that replace or suppress a model decision because text
  matched a hand-written rule;
- hidden "known safe", "known dangerous", or "unknown means interactive"
  classifications based on wording.

Changing the match from substring to full-text, adding more languages, or
making the list positive-only does not make such a component acceptable.

## Allowed deterministic code

Code may enforce facts that require no interpretation:

- authentication and exact identity or capability checks;
- authorization against explicit structured identities and permissions;
- schema, type, range, and enum validation;
- exact protocol action dispatch after the model selected the action;
- hashes, signatures, replay protection, idempotency, and append-only
  invariants;
- filesystem, process, database, network, and transaction-state invariants;
- rate, time, iteration, memory, and resource bounds;
- exact address resolution for a structured ID or canonical key selected by
  the model.

These mechanisms may reject an invalid or unauthorized structured operation.
They must not decide what the user meant.

## Required interaction shape

The model receives the relevant context and chooses a structured operation.
The deterministic layer validates that operation and either executes it or
returns precise factual evidence to the model. The model then decides how to
adapt.

```text
user/context
    -> LLM interpretation and decision
    -> structured operation
    -> mechanical validation/execution
    -> factual result
    -> LLM interpretation
```

When a capability needs an approval or risk field, that field is authored by
the model or supplied as explicit user/administrator authority. It must not be
derived by scanning the operation's prose or shell/code text.

## Review gate

A change fails review if non-model code reads free-form text and uses its
wording to choose a route, permission posture, approval path, autonomous
answer, or business action. Exact protocol parsing is acceptable only when the
input is already a structured protocol value and the parser does not infer
meaning from prose.

This rule applies to core, gateway, plugins, skills with executable helpers,
operational overlays, and deployment-specific code.
