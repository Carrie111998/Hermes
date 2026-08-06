# Delegation Contracts Pilot

## Zweck

Der Pilot führt strikte, Hermes-native Verträge für Delegationsaufgaben ein, ohne
bestehende Prompt- oder Tool-Schemas zu verändern.

## Artefakte

- `tools/delegation_contracts.py`
  - `LaneTask`
  - `LaneResult`
  - `ReviewDecision`
  - `validate_contract()`
- `schemas/delegation/LaneTask.json`
- `schemas/delegation/LaneResult.json`
- `schemas/delegation/ReviewDecision.json`
- `tests/test_delegation_contracts.py`

## Designentscheidung

Die bestehenden Hermes-Flows verwenden bereits freie Dictionaries für
`delegate_task` und persistieren bei asynchroner Delegation ein `task_json` sowie
ein `result_json` in `async_delegations`. Der Pilot validiert diese Daten noch
nicht automatisch in der Runtime. Das ist absichtlich der nächste, getrennte
Schritt: Erst werden die Verträge unabhängig getestet, danach erfolgt die
read-only Audit-Anbindung und erst anschließend eine Runtime-Integration.

## Read-only-Abgleich

- `delegate_task` akzeptiert weiterhin die bestehende öffentliche Tool-Semantik.
- Async-Persistenz speichert `goal`, `goals`, `context`, `toolsets`, `role`,
  `model` und `is_batch` als Task-Payload.
- Das vorhandene Result-Shaping ist nicht identisch mit `LaneResult` und braucht
  einen Upcaster, bevor es validiert werden kann.
- Ein direktes Einhängen in `delegate_tool.py` wäre daher aktuell ein
  Behavior-Change und wird im Pilot nicht vorgenommen.

## Testlauf

```text
python3 -m pytest tests/test_delegation_contracts.py -q
6 passed in 0.14s
```

## Nächster Integrationsschritt

Ein Adapter sollte aus dem bestehenden Async-Task-Payload ein `LaneTask` bauen
und die vorhandene Child-Ausgabe in ein `LaneResult` normalisieren. Der Adapter
muss zunächst nur beobachten und bei ungültigen Daten warnen. Erst nach einem
belegten Durchlauf ohne False Positives darf daraus ein fail-closed Gate werden.
