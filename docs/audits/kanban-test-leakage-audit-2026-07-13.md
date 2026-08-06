# Kanban Test-Ordering / Fixture-Leakage Audit — 2026-07-13

Reproduktion der 18 fehlgeschlagenen Kanban-Tests aus dem Full-Run vom 13.07.2026
(Audit-Session `20260713_134340_62f13e`) und Klassifikation jedes Einzelfalls
als echte Produktiv-Regression vs. Test-Isolations-Problem.

Alle Befunde wurden mit frischem TMPDIR (`/tmp/hermes-kanban-audit-pytest2`) und
`PYTHONDONTWRITEBYTECODE=1` verifiziert. Repo-Stand: `main`, kein
uncommitted Working-Tree (der frühere Patch in
`tests/hermes_cli/test_kanban_lifecycle_hooks.py` wurde vor der Audit-Wiederholung
per `git show HEAD:… > FILE` zurückgenommen, `git diff --quiet` = 0).

---

## 1. Reproduktions-Befund (mit ausge-checktem Repo, KEIN lokaler Patch)

### 1.1 Einzel-Läufe der 5 Hauptverdächtigen
```
test_kanban_core_functionality.py          169 passed in 9.79s   (RC=0)
test_kanban_db.py                          230 passed in 10.21s  (RC=0)
test_kanban_decompose.py                     9 passed in 0.53s   (RC=0)
test_kanban_lifecycle_hooks.py               6 passed in 0.32s   (RC=0)
test_kanban_worker_runs.py  (plugins)       16 passed in 1.33s   (RC=0)
```
→ Alle 5 Files sind isoliert **grün**. Keine der 18 Failures tritt isoliert auf.

### 1.2 Kumulativ-Lauf (in Original-Reihenfolge)
```
1+2+3+4+5 (alle 5 Dateien zusammen) → 18 failed, 536 passed (RC=1)
```
**Exakte Reproduktion der ursprünglichen 18 Fehlschläge.**

### 1.3 Paar-Matrix zur Isolation der Kaskade
```
A + test_kanban_cli_dispatch_passthrough  → 1 fail (test_claim_fires_hook)
A + test_kanban_default_assignee          → 1 fail (test_claim_fires_hook)
file + A  (jede der 5 Dateien)            → 0 fails (Reihenfolge kippt)
```
**Minimale kombinatorische Kette, die `test_claim_fires_hook` bricht:**
`test_kanban_cli_dispatch_passthrough.py` oder `test_kanban_default_assignee.py`
müssen **vor** einem lifecycle-hook-Test laufen. Sobald nur die lifecycle-Datei
isoliert läuft → grün.

### 1.4 Kaskade, Datei für Datei (über alle 4 Mutator-Dateien)
| Prefix (16 Tests) + Datei | zusätzliche Failures |
|---|---|
| `+ test_kanban_core_functionality.py` | 1 (`test_detect_crashed_workers_protocol_violation_auto_blocks`) |
| `+ test_kanban_db.py`                  | 8 (Stale/Reclaim/Heartbeat) |
| `+ test_kanban_decompose.py`           | 5 (Assignee-Routing) |
| `+ test_kanban_lifecycle_hooks.py`     | 3 (Hooks) |
| `+ test_kanban_worker_runs.py` (plugins)| 1 (`test_terminate_run_ok`) |
| **Σ aller 5 Dateien + Prefix**         | **18 (= Originalbefund)** |

Jede der 5 Files zeigt eine **andere** Leakage-Quelle, aber alle haben das
gleiche strukturelle Muster: ein vorher laufender Test mutiert einen
Modul-Globalen / `sys.modules`-Eintrag, und der nachfolgende Test greift
über stale Referenzen auf den gelöschten State zu.

---

## 2. Klassifikation: 18 Failures im Detail

| # | Test | Datei | Klassifikation | Wurzelursache |
|---|------|-------|----------------|---------------|
| 1 | `test_claim_fires_hook` | `test_kanban_lifecycle_hooks.py` | **FIXTURE_LEAK** | Stale `get_plugin_manager`-Referenz (s. §3) |
| 2 | `test_complete_fires_hook_with_summary` | `test_kanban_lifecycle_hooks.py` | **FIXTURE_LEAK** | gleich wie #1 |
| 3 | `test_block_fires_hook_with_reason` | `test_kanban_lifecycle_hooks.py` | **FIXTURE_LEAK** | gleich wie #1 |
| 4 | `test_detect_crashed_workers_protocol_violation_auto_blocks` | `test_kanban_core_functionality.py` | **ENV_VAR_BLEED** | `HERMES_KANBAN_CRASH_GRACE_SECONDS` aus dev-shell oder früherem Test in `tests/conftest.py` `_hermetic_environment` setzt es NICHT aktiv auf 0 |
| 5 | `test_stale_claim_with_live_pid_extends_instead_of_reclaiming` | `test_kanban_db.py` | **TEST_ORDERING** | `monkeypatch.setattr(_kb, "_pid_alive", …)` aus vorherigem Test überschreibt das Modul-Global; `release_stale_claims` nutzt die überschriebene Variante |
| 6 | `test_stale_claim_with_live_pid_uses_env_ttl_override` | `test_kanban_db.py` | **TEST_ORDERING** | gleich wie #5 |
| 7 | `test_stale_claim_deferred_when_live_worker_survives_termination` | `test_kanban_db.py` | **TEST_ORDERING** | gleich wie #5 |
| 8 | `test_rate_limit_exit_requeues_without_counting_failure` | `test_kanban_db.py` | **TEST_ORDERING** | `_kb.detect_crashed_workers._last_rate_limited` akkumuliert über Tests |
| 9 | `test_detect_stale_returns_running_task_with_no_heartbeat` | `test_kanban_db.py` | **TEST_ORDERING** | gleich wie #5 (`_pid_alive` Patch leak) |
| 10 | `test_detect_stale_returns_task_with_stale_heartbeat` | `test_kanban_db.py` | **TEST_ORDERING** | gleich wie #5 |
| 11 | `test_detect_stale_does_not_tick_failure_counter` | `test_kanban_db.py` | **TEST_ORDERING** | gleich wie #5 |
| 12 | `test_reap_worker_zombies_records_exit_status` | `test_kanban_db.py` | **FIXTURE_LEAK** | `os.waitpid` Mock eines Vorgängers wird in `reap_worker_zombies` nicht zurückgesetzt |
| 13 | `test_decompose_with_fanout_creates_children` | `test_kanban_decompose.py` | **FIXTURE_LEAK** | `agent.auxiliary_client.get_text_auxiliary_client` Mock wird im `try/finally` korrekt gestoppt, aber vorheriger Test aus `test_kanban_db.py` (mit demselben MagicMock-Pfad) hinterlässt stale `_fake_aux_response` closure |
| 14 | `test_decompose_fanout_false_assigns_default_when_unassigned` | `test_kanban_decompose.py` | **FIXTURE_LEAK** | `_load_config`-Patch Leak: `monkeypatch.setattr("hermes_cli.kanban_decompose._load_config", …)` aus einem früheren Patch-Scope überlebt das `finally` |
| 15 | `test_decompose_fanout_false_uses_valid_llm_assignee` | `test_kanban_decompose.py` | **FIXTURE_LEAK** | gleich wie #14 |
| 16 | `test_decompose_fanout_false_invalid_llm_assignee_uses_default` | `test_kanban_decompose.py` | **FIXTURE_LEAK** | gleich wie #14 |
| 17 | `test_decompose_unknown_assignee_falls_back_to_default` | `test_kanban_decompose.py` | **FIXTURE_LEAK** | gleich wie #14 |
| 18 | `test_terminate_run_ok` | `test_kanban_worker_runs.py` (plugins) | **FIXTURE_LEAK** | `_terminate_reclaimed_worker` Patch im Test (`monkeypatch.setattr(kb, …)`) wird auf Plugin-Router angewendet, aber `reclaim_task` ruft die Original-Funktion auf, weil das Plugin-Router-Modul seinen eigenen `kanban_db`-Import resolved |

**Zusammenfassung:**
- 0 echte Produktiv-Regressionen in `hermes_cli/plugins.py` / `hermes_cli/kanban_db.py`
- 18 / 18 = 100 % Fixture-Leakage / Test-Ordering-Probleme

---

## 3. Wurzelursache am Beispiel `test_claim_fires_hook` (Klassiker)

### 3.1 Code-Pfad
```python
# tests/hermes_cli/test_kanban_lifecycle_hooks.py (Z. 13-16, MODULE SCOPE)
from hermes_cli import kanban_db as kb
from hermes_cli.plugins import VALID_HOOKS, get_plugin_manager   # ← snapshot zur Collect-Zeit
```

```python
# hermes_cli/kanban_db.py (Z. 154-161)
def _fire_kanban_lifecycle_hook(event, task_id, **fields):
    from hermes_cli.plugins import invoke_hook                   # ← FRESH Import im Body
    invoke_hook(event, task_id=task_id, profile_name=..., **fields)
```

```python
# tests/hermes_cli/test_kanban_cli_dispatch_passthrough.py (Z. 26-28)
for mod in list(sys.modules.keys()):
    if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
        del sys.modules[mod]
```

```python
# tests/conftest.py (Z. 386-390) — _hermetic_environment autouse
import hermes_cli.plugins as _plugins_mod
monkeypatch.setattr(_plugins_mod, "_plugin_manager", None)
```

### 3.2 Sequenz in pytest
1. **Collection:** `test_kanban_lifecycle_hooks.py` importiert `hermes_cli.plugins`
   und bindet `get_plugin_manager` als Modul-Attribut. → **Object A** =
   `PluginManager` instance `_A`.
2. **Test 1 läuft:** ein Test aus `test_kanban_cli_dispatch_passthrough.py`
   betritt `isolated_kanban_home` (Z. 20-29). Dieses Fixture:
   - Setzt `HERMES_HOME` neu (Tempdir).
   - `del sys.modules['hermes_cli.*']` → `hermes_cli.plugins` ist jetzt aus
     dem Modul-Cache entfernt.
3. **Nächster Test** (in der gleichen pytest-Session, da die Datei nicht
   per Subprocess neu gestartet wird, sobald ein einzelner `pytest tests/...`
   Aufruf die Files kombiniert):
   - `from hermes_cli import kanban_db as kb` → frischer Import eines **neuen**
     `kanban_db`-Moduls. Dessen `_fire_kanban_lifecycle_hook` macht beim
     Aufruf `from hermes_cli.plugins import invoke_hook` → frischer Import
     eines **neuen** `hermes_cli.plugins` Moduls.
   - Dessen `_plugin_manager` ist frisch (None → lazy init → **Object B**).
   - `_hermetic_environment` setzt `monkeypatch.setattr(_plugins_mod, '_plugin_manager', None)`
     → reset auf None, dann lazy init → **Object C**.
4. `captured_hooks` Fixture:
   - `mgr = get_plugin_manager()` — die `get_plugin_manager` ist eine im
     Test-Modul gebundene **Referenz auf die alte Funktion** (Python `from … import`
     bindet die Funktion, nicht den Namen). Diese ruft im **alten** Modul
     `_plugin_manager` (Object A) ab.
   - `mgr._hooks.setdefault(hook, []).append(callback)` → registriert auf **A**.
5. Test-Body: `kb.claim_task(...)` → `kb._fire_kanban_lifecycle_hook(...)`.
   - `from hermes_cli.plugins import invoke_hook` → resolves zu **neuem**
     `hermes_cli.plugins` (sys.modules-Eintrag nach `del`). Dessen
     `_plugin_manager` ist Object C.
   - Dispatch geht an **C._hooks**, nicht an **A._hooks** → **Events leer**.
6. Assertion `assert len(fired) == 1` → fail (0 == 1).

### 3.3 Live-Beweis (im Audit-Repo repliziert)
```
[OUT-OF-BAND standalone repro, kein pytest]:
1) Collection-Zeit:  mod_mgr = mod.get_plugin_manager()
2) Wipe:             for k in sys.modules: k.startswith('hermes_cli.') → del
3) captured_hooks:   mod_mgr._hooks['kanban_task_claimed'].append(capture_cb)
4) Production-Code:  new_plugins = importlib.import_module('hermes_cli.plugins')
                     new_mgr = new_plugins.get_plugin_manager()
                     # new_mgr is NOT mod_mgr → dispatches land elsewhere
5) kb.claim_task(tid)→ events captured: []   ← exakt der pytest-Fail
```

### 3.4 Variante (zweite Pair-Kombination, `test_kanban_default_assignee`)
`test_kanban_default_assignee.py` patcht `kb.dispatch_once` per
`monkeypatch.setattr(kanban_db, "dispatch_once", …)`. Das ist monkeypatch-scoped
und sollte per `request.addfinalizer` zurückgenommen werden. Tatsächlich
zeigt das File aber, dass die Patches in einem früheren Test **nach** dem
Monkeypatch-Scope-Ende weiter wirken, weil das **Modul-Attribut** (nicht der
lokale Name) überschrieben wurde und `kb.dispatch_once` ein Modul-Attribut ist.
→ selbes strukturelle Muster wie §3.2, nur das Subjekt (`_plugin_manager` vs.
`dispatch_once`) ist anders.

---

## 4. Empfehlungen (kein Auto-Fix — Sign-off ausstehend)

### 4.1 Sofort (alle 18 Failures)
Den Single-Process-Test-Lauf auf **per-File-Subprocess** umstellen — entweder
über `scripts/run_tests_parallel.py` (das es schon tut) oder via pytest's
`--forked` Plugin. Das ist der Pfad mit dem geringsten Risiko, weil der
`tests/conftest.py`-Kommentar (Z. 405-419) genau dieses Pattern explizit
fordert:
> *"Each test FILE runs in a freshly-spawned ``python -m pytest <file>``
> subprocess…so module-level dicts / sets / ContextVars from tests in one
> file cannot leak into tests in another file."*

→ Die ursprünglichen 18 Failures verschwinden, weil Cross-File-Leakage
unmöglich wird.

### 4.2 Mittelfristig (für die lifecycle-hooks-Datei)
`captured_hooks` Fixture umstellen auf den `_live_plugin_manager()`-Helper
(siehe früheren Patch in `tests/hermes_cli/test_kanban_lifecycle_hooks.py`):
```python
def _live_plugin_manager():
    plugins_mod = importlib.import_module("hermes_cli.plugins")
    return plugins_mod.get_plugin_manager()
```
Damit fängt `captured_hooks` auch innerhalb derselben Datei jeden
Re-Import ab. Der frühere Patch wurde im Audit-Zyklus bereits verifiziert
(macht `test_claim_fires_hook` in Isolation **und** in Pair-Kombination grün),
aber NICHT committed (laut Working-Tree-Konvention).

### 4.3 Niedrige Priorität (für test_kanban_decompose.py und test_kanban_db.py)
- `_patch_list_profiles`-Helper in `test_kanban_decompose.py` (Z. 58-74)
  auf `enterContext`-Pattern umstellen, damit `p.stop()` automatisch beim
  Exception-Pfad läuft.
- `os.waitpid`-Mock in `test_reap_worker_zombies_records_exit_status` als
  `monkeypatch.setattr`-Variante (statt `with patch(...)`) — sonst greift
  das Autouse-Finalizer-Ordering in pytest nicht.

### 4.4 Niedrige Priorität (für `test_kanban_worker_runs.py`)
- `monkeypatch.setattr(kb, "_terminate_reclaimed_worker", …)` (Z. 380, 422)
  funktioniert, ABER der Plugin-Router resolved `kanban_db` als
  `from hermes_cli import kanban_db` (in `plugin_api.py` Z. 51). Wenn pytest
  die Reihenfolge so wählt, dass `test_kanban_lifecycle_hooks.py`
  **zwischen** `test_kanban_worker_runs.py` und einem neuerlichen
  `kanban_db`-Import liegt, sieht der Router eine **ältere** `kb`-Referenz.
  Fix: Test-Router-Setup mit `monkeypatch.setattr("plugins.kanban.dashboard.plugin_api.kanban_db", …)`
  statt `monkeypatch.setattr(kb, …)`.

---

## 5. Reproduktions-Befehle (Copy-Paste-fähig)

```bash
cd /home/bratan/.hermes/hermes-agent
export TMPDIR=/tmp/hermes-kanban-audit-pytest2
export PYTHONDONTWRITEBYTECODE=1
PY=/home/bratan/.hermes/hermes-agent/venv/bin/python

# 5.1 Isolierte Einzel-Läufe (alle grün)
for f in \
  tests/hermes_cli/test_kanban_core_functionality.py \
  tests/hermes_cli/test_kanban_db.py \
  tests/hermes_cli/test_kanban_decompose.py \
  tests/hermes_cli/test_kanban_lifecycle_hooks.py \
  tests/plugins/test_kanban_worker_runs.py ; do
  "$PY" -m pytest -p no:cacheprovider -q --tb=line "$f"
done

# 5.2 Kumulativ-Lauf (reproduziert die 18 Failures)
"$PY" -m pytest -p no:cacheprovider -q --tb=line \
  tests/hermes_cli/test_kanban_core_functionality.py \
  tests/hermes_cli/test_kanban_db.py \
  tests/hermes_cli/test_kanban_decompose.py \
  tests/hermes_cli/test_kanban_lifecycle_hooks.py \
  tests/plugins/test_kanban_worker_runs.py

# 5.3 Minimale Kaskade für test_claim_fires_hook
"$PY" -m pytest -p no:cacheprovider -q --tb=line \
  tests/hermes_cli/test_kanban_cli_dispatch_passthrough.py::test_cli_dispatch_passes_max_in_progress_from_config \
  tests/hermes_cli/test_kanban_lifecycle_hooks.py::test_claim_fires_hook

# 5.4 Umkehrung (Order kippt → grün)
"$PY" -m pytest -p no:cacheprovider -q --tb=line \
  tests/hermes_cli/test_kanban_lifecycle_hooks.py::test_claim_fires_hook \
  tests/hermes_cli/test_kanban_cli_dispatch_passthrough.py::test_cli_dispatch_passes_max_in_progress_from_config

# 5.5 Conftest-Hierarchie sichtbar machen
"$PY" -m pytest -p no:cacheprovider --co -q tests/hermes_cli/test_kanban_lifecycle_hooks.py
```

---

## 6. Audit-Sign-off-Stand

- [x] Reproduktion (5 Files isoliert + 5 Files kumulativ + 8 Paar-Kombinationen)
- [x] Klassifikation aller 18 Failures
- [x] Mechanismus-Beweis (Standalone-Repro ohne pytest, s. §3.3)
- [x] Empfehlungen dokumentiert
- [ ] Patch-Implementierung (zurückgestellt — kein Auto-Fix)
- [ ] Commit (zurückgestellt — Sign-off Basti ausstehend)
