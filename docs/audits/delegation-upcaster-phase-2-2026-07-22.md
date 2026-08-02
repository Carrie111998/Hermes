# Phase 2: Read-only-Upcaster

**Datum:** 2026-07-22  
**Scope:** Bestehende Async-Delegationspayloads in `LaneTask` und `LaneResult` übersetzen, ohne Hermes-Runtime oder `state.db` zu verändern.

## Artefakte

- `tools/delegation_upcaster.py`
  - `upcast_async_task()` unterstützt Single- und Batch-Payloads.
  - `upcast_async_result()` unterstützt Einzelresultate, Batch-Resultate und unbekannte Ergebnisse.
  - Ungültige oder nicht verifizierbare Resultate werden als `blocked` markiert.
- `scripts/audit_delegation_contracts.py`
  - liest `async_delegations` ausschließlich lesend
  - schreibt nur einen separaten JSON-Report
- `tests/test_delegation_upcaster.py`
  - sechs Tests für Single, Batch, Resultate und Fehlerpfade
- `/home/bratan/20-Workspace/results/delegation-contract-audit-2026-07-22.json`
  - Ergebnis des Live-Audits

## Live-Audit

Quelle:

```text
/home/bratan/.hermes/state.db
```

Ergebnis:

| Kennzahl | Ergebnis |
|---|---:|
| Gelesene Async-Delegationen | 51 |
| Task-Upcasts gültig | 51 |
| Task-Upcasts ungültig | 0 |
| Result-Upcasts gültig | 51 |
| Result-Upcasts ungültig | 0 |
| Als `completed` erkennbar | 50 |
| Als `blocked` markiert | 1 |

Der eine `blocked`-Datensatz war kein Validatorfehler. Er hatte einen unbekannten bzw. nicht verifizierbaren Abschlussstatus. Das ist genau der Fall, den Phase 2 sichtbar machen soll.

## Verifikation

```text
python3 -m pytest tests/test_delegation_upcaster.py tests/test_delegation_contracts.py tests/tools/test_delegate.py -q
170 passed in 4.84s
```

## Sicherheitsgrenze

Noch nicht aktiviert:

- kein Eingriff in `delegate_tool.py`
- kein Eingriff in `async_delegation.py`
- kein Schreiben in `state.db`
- keine Blockade produktiver Delegationen
- keine Änderung an Prompt- oder Tool-Schemas

## Befund für Phase 3

Der Upcaster funktioniert auf den vorhandenen 51 Async-Datensätzen ohne invalides Task- oder Result-Objekt. Ein Datensatz bleibt fachlich `blocked`, weil sein Abschluss nicht verifiziert werden kann.

Damit ist der nächste sinnvolle Schritt Contract-Observability:

- Warnungen nach Fehlerklasse gruppieren
- `blocked`-Ursachen genauer klassifizieren
- Batch-Resultate und Einzelresultate getrennt zählen
- erst danach ein Soft-Gate im Runtime- oder Delivery-Pfad erwägen
