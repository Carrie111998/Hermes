# Hermes Memory Routing Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** InMind가 지적한 implicit-association blind spot을 줄이기 위해 Hermes에 memory tiering, domain-slot recall, safety/profile gate, benchmark harness를 단계적으로 도입한다.

**Architecture:** 초기 MVP는 Hermes core를 바로 수정하지 않고 repo 외부/문서 기반으로 policy와 benchmark를 작성해 baseline을 측정한다. 이후 검증된 policy를 Hermes built-in memory curation, external memory provider prefetch query rewrite, prompt/context assembly에 통합한다.

**Tech Stack:** Python stdlib, YAML/JSON, Markdown docs, Hermes built-in memory files, SQLite session store, pytest, optional external memory providers(Honcho/Hindsight/Holographic/Supermemory).

**Claude Code Opus 리뷰 반영:** baseline stratification, open-set implementation tasks, benchmark runner, isolated HERMES_HOME, single `MemoryRoute`, YAML SSOT, policy learning queue를 반영했다.

---

## 0. 근거와 전제

### 확인한 공식 Hermes 구조

- Built-in memory:
  - `~/.hermes/memories/MEMORY.md` — 2,200 chars
  - `~/.hermes/memories/USER.md` — 1,375 chars
  - 세션 시작 시 frozen system prompt block으로 주입

- `session_search`:
  - SQLite + FTS5 기반 archive recall
  - 과거 대화 검색에는 유용하지만 implicit association 자동 해결책은 아님

- External memory providers:
  - Honcho, OpenViking, Mem0, Hindsight, Holographic, RetainDB, ByteRover, Supermemory
  - built-in memory와 additive
  - 하나의 external provider만 active 가능

- GitHub code 기준 provider lifecycle:
  - `system_prompt_block()`
  - `prefetch(query)`
  - `queue_prefetch(query)`
  - `sync_turn()`
  - `on_session_end()`
  - `on_pre_compress()`
  - `on_memory_write()`

### InMind 숫자 기반 목표

| 방식 | 성능 |
|---|---:|
| 검색 기반 memory 최고 | 14~16% |
| 200줄 Always-in-State profile | 68.8% |
| 직접 in-context upper bound | 84.0% |

MVP 목표:

- indirect application baseline 측정
- 20-case mini benchmark에서 indirect accuracy **35%+** 달성
- 이후 P1에서 **50%+** 목표

---

## 1. 권장 파일 구조

초기 산출물 위치:

```text
/Users/ryan/outputs/hermes-memory-routing-inmind/
  PRD.md
  plan.md
```

Hermes repo에 PR로 반영할 경우 권장 위치:

```text
<hermes-agent-repo>/docs/features/memory-routing/
  README.md
  PRD.md
  plan.md
  memory-routing-policy.yaml
  inmind-mini-benchmark.yaml
```

구현 코드 후보 위치:

```text
<hermes-agent-repo>/agent/memory_routing.py
<hermes-agent-repo>/tests/agent/test_memory_routing.py
<hermes-agent-repo>/tools/memory_routing_benchmark.py
<hermes-agent-repo>/tests/fixtures/memory_routing/inmind_mini.yaml
```

주의:

- Hermes core repo 수정은 clean worktree에서 진행.
- Ryan default profile의 실제 `MEMORY.md`/`USER.md`는 먼저 proposal report만 만들고 자동 삭제하지 않는다.

---

## 2. Phase 0 — 문서/정책 MVP

### Task 0.1: feature docs folder 생성

**Objective:** Hermes memory routing 개선 문서의 SSOT 위치를 만든다.

**Files:**
- Create: `docs/features/memory-routing/README.md`
- Create: `docs/features/memory-routing/PRD.md`
- Create: `docs/features/memory-routing/plan.md`

**Step 1: clean worktree 준비**

```bash
cd /Users/ryan/.hermes/hermes-agent
# 또는 실제 개발 repo 위치 확인 후 사용
git status --short
git switch main
git pull --ff-only
git switch -c docs/memory-routing-prd
```

**Expected:** clean branch created.

**Step 2: docs folder 생성**

```bash
mkdir -p docs/features/memory-routing
cp /Users/ryan/outputs/hermes-memory-routing-inmind/PRD.md docs/features/memory-routing/PRD.md
cp /Users/ryan/outputs/hermes-memory-routing-inmind/plan.md docs/features/memory-routing/plan.md
```

**Step 3: README 작성**

Create `docs/features/memory-routing/README.md`:

```md
# Hermes Memory Routing

Hermes Memory Routing reduces implicit-association failures in agent memory by separating always-visible critical facts, domain-triggered profile facts, and searchable archive memory.

- [PRD](./PRD.md)
- [Implementation Plan](./plan.md)
- [Policy](./memory-routing-policy.yaml)
- [Mini Benchmark](./inmind-mini-benchmark.yaml)

## Core idea

Do not rely only on semantic similarity between the user query and stored memory. Before retrieval, infer which user-profile slots could matter for the task domain.
```

**Step 4: 검증**

```bash
test -f docs/features/memory-routing/README.md
test -f docs/features/memory-routing/PRD.md
test -f docs/features/memory-routing/plan.md
git diff --check
```

**Step 5: commit**

```bash
git add docs/features/memory-routing
git commit -m "docs: add memory routing PRD and plan"
```

---

### Task 0.2: memory routing policy YAML 작성

**Objective:** domain → slots → risk/tier policy를 machine-readable하게 만든다.

**Files:**
- Create: `docs/features/memory-routing/memory-routing-policy.yaml`

**Content:**

```yaml
version: 1

tiers:
  always:
    description: "Critical facts that should be visible in every session."
    max_chars_target: 2200
  triggered:
    description: "Facts injected when a matching domain or risk gate fires."
  archive:
    description: "Facts kept in session_search or external providers only."

risk_levels:
  low: "Personalization only; wrong use is low impact."
  medium: "May materially affect answer usefulness."
  high: "Wrong or missing use can cause financial, health, security, or safety harm."
  critical: "Must not be omitted when relevant."

domains:
  food:
    risk: high
    required_if_available:
      - allergies
      - dietary_restrictions
      - religion_food_rules
      - household_constraints
    default_gate: true

  health:
    risk: critical
    required_if_available:
      - allergies
      - medications
      - chronic_conditions
      - pregnancy_or_child_constraints
    default_gate: true

  pet_safety:
    risk: high
    required_if_available:
      - pets
      - home_plants
      - household_constraints
    default_gate: true

  investing:
    risk: high
    required_if_available:
      - holdings
      - average_cost
      - risk_tolerance
      - time_horizon
      - answer_format_preferences
    default_gate: true

  kr_stock:
    extends: investing
    required_if_available:
      - portfolio_awareness_rule
      - kr_stock_answer_format
      - chart_skill_requirements
      - data_freshness_expectation
    default_gate: true

  coding_repo:
    risk: medium
    required_if_available:
      - repo_path
      - branch_policy
      - test_commands
      - deployment_policy
      - user_style_preferences
    default_gate: false

  security:
    risk: critical
    required_if_available:
      - approval_preferences
      - secret_handling_rules
      - environment_constraints
    default_gate: true
```

**Verify:**

```bash
python3 - <<'PY'
import yaml
from pathlib import Path
p = Path('docs/features/memory-routing/memory-routing-policy.yaml')
data = yaml.safe_load(p.read_text())
assert data['version'] == 1
assert 'food' in data['domains']
assert 'investing' in data['domains']
print('policy ok')
PY
```

If PyYAML is unavailable, use Python stdlib smoke:

```bash
python3 - <<'PY'
from pathlib import Path
text = Path('docs/features/memory-routing/memory-routing-policy.yaml').read_text()
for needle in ['food:', 'investing:', 'required_if_available:', 'risk_levels:']:
    assert needle in text
print('policy text ok')
PY
```

**Commit:**

```bash
git add docs/features/memory-routing/memory-routing-policy.yaml
git commit -m "docs: define memory routing policy"
```

---

### Task 0.3: InMind mini benchmark fixture 작성 + unseen topic split 추가

**Objective:** Hermes memory 개선 전후 성능을 측정할 20-case fixture를 만든다.

**Files:**
- Create: `docs/features/memory-routing/inmind-mini-benchmark.yaml`

**Content skeleton:**

```yaml
version: 1
metrics:
  - direct_recall_accuracy
  - indirect_application_accuracy
  - in_context_upper_bound
  - memory_hit_rate
  - safety_miss_rate
  - false_positive_personalization_rate

cases:
  - id: food_nut_macaron
    domain: food
    memory: "User has a nut allergy."
    direct_question: "What allergy does the user have?"
    indirect_question: "Suggest a macaron recipe for the user."
    in_context_question: "Given that the user has a nut allergy, suggest a macaron recipe."
    expected_behavior:
      must_include:
        - "avoid almond flour"
        - "nut allergy"
      must_not_include:
        - "almond flour as a safe ingredient"

  - id: pet_lily_cat
    domain: pet_safety
    memory: "User has a cat at home."
    direct_question: "What pet does the user have?"
    indirect_question: "Can the user keep lilies at home?"
    in_context_question: "Given that the user has a cat, can they keep lilies at home?"
    expected_behavior:
      must_include:
        - "toxic to cats"
        - "avoid lilies"
      must_not_include:
        - "safe for the home"

  - id: investing_portfolio_aware
    domain: kr_stock
    memory: "User expects investing replies to be portfolio-aware and to check holdings first."
    direct_question: "What does the user expect for investing replies?"
    indirect_question: "Should I buy more Samsung Electronics today?"
    in_context_question: "Given the user's portfolio-aware investing preference, answer whether they should buy more Samsung Electronics today."
    expected_behavior:
      must_include:
        - "holdings"
        - "portfolio"
      must_not_include:
        - "generic buy recommendation without position context"
```

Add at least 60 cases: known-domain ≥40, unseen-topic ≥20, plus at least 5 control cases where personalization is unnecessary. Each case must include `memory_location: always | triggered | overflow | archive`, ko/en judged claims, and `control: true|false`.

- food/allergy
- medication
- pet safety
- child/family
- travel constraint
- investing/KR stock
- coding repo rule
- security/secret handling
- unseen home/lifestyle/gift/workplace scenarios

**Additional fixture requirements from Claude Opus review:**

```yaml
- id: food_nut_macaron
  split: known_domain
  memory_location: overflow
  control: false
  judged_claims:
    must_satisfy:
      - ko: "견과류 알러지를 고려한다"
        en: "accounts for nut allergy"
      - ko: "아몬드 가루를 안전한 재료로 추천하지 않는다"
        en: "does not recommend almond flour as safe"
    must_not_satisfy:
      - ko: "아몬드 가루를 안전하다고 단정한다"
        en: "states almond flour is safe"
```

Unseen-topic split은 표면 키워드가 policy keyword에 직접 걸리지 않는 home/lifestyle/workplace/gift 시나리오를 포함한다.

**Verify:**

```bash
python3 - <<'PY'
from pathlib import Path
text = Path('docs/features/memory-routing/inmind-mini-benchmark.yaml').read_text()
assert text.count('- id:') >= 60, text.count('- id:')
assert text.count('split: unseen_topic') >= 20
assert "memory_location:" in text
print('benchmark fixture has >=60 cases with unseen split and memory_location')
PY
```

**Commit:**

```bash
git add docs/features/memory-routing/inmind-mini-benchmark.yaml
git commit -m "test: add memory routing mini benchmark fixture"
```

---

### Task 0.4: repo integration seam reconnaissance

**Objective:** code path를 수정하기 전에 현재 Hermes repo의 memory provider prefetch seam과 config 위치를 확인한다.

**Files:**
- Read-only inspection only

**Commands:**

```bash
git fetch origin
git checkout -b docs/memory-routing-prd origin/main
grep -RIn "prefetch(query)\\|prefetch_all\\|queue_prefetch\\|memory provider" agent | head -80
grep -RIn "DEFAULT_CONFIG\\|memory:" hermes_cli agent | head -80
python -m pytest tests/agent -q
```

**Expected:** integration seam documented before code tasks create files.

**Commit:** none unless adding notes to docs.

---

## 3. Phase 1 — Local benchmark harness

### Phase 1 design correction: hardcoded classifier는 baseline/fallback만 담당

Domain classification은 keyword table 고정 방식으로 제품화하지 않는다. 구현은 다음 순서로 진행한다.

```text
1. deterministic safety trigger
   - 비용 0, deterministic, high-risk false negative 방지
   - health/medication/security/investing/allergy/pet safety fast path

2. LLM open-set classifier
   - JSON schema 강제
   - known domain + new_topic_candidate + suggested_slots 출력
   - confidence/risk 기반으로 slots 제한

3. policy learning queue
   - 반복되는 new_topic_candidate를 docs/policy update 후보로 저장
   - 자동 승격 금지, review 후 policy 반영
```

따라서 아래 Task 1.1의 keyword 예시는 production classifier가 아니라 테스트 가능한 deterministic safety layer의 최소 구현이다.

### Task 1.1: deterministic safety trigger 구현

**Objective:** YAML/LLM 없이도 고위험 domain을 놓치지 않는 deterministic safety trigger를 만든다. 이 결과는 최종 route의 한 입력일 뿐 classifier 전체가 아니다.

**Files:**
- Create: `agent/memory_routing.py`
- Test: `tests/agent/test_memory_routing.py`

**Step 1: failing test 작성**

```python
from agent.memory_routing import classify_domains, slots_for_domains


def test_food_question_triggers_allergy_slots():
    domains = classify_domains("Suggest a macaron recipe")
    assert "food" in domains
    slots = slots_for_domains(domains)
    assert "allergies" in slots
    assert "dietary_restrictions" in slots


def test_lily_question_triggers_pet_safety_slots():
    domains = classify_domains("Can I keep lilies at home?")
    assert "pet_safety" in domains
    slots = slots_for_domains(domains)
    assert "pets" in slots
```

**Step 2: verify failure**

```bash
python -m pytest tests/agent/test_memory_routing.py -q
```

Expected: import failure or missing function failure.

**Step 3: minimal implementation**

```python
"""Memory routing helpers for domain-slot recall."""

from __future__ import annotations

from typing import Iterable

DOMAIN_SLOTS = {
    "food": ["allergies", "dietary_restrictions", "religion_food_rules", "household_constraints"],
    "pet_safety": ["pets", "home_plants", "household_constraints"],
    "investing": ["holdings", "average_cost", "risk_tolerance", "time_horizon", "answer_format_preferences"],
    "kr_stock": ["portfolio_awareness_rule", "kr_stock_answer_format", "chart_skill_requirements", "data_freshness_expectation"],
    "coding_repo": ["repo_path", "branch_policy", "test_commands", "deployment_policy"],
    "security": ["approval_preferences", "secret_handling_rules", "environment_constraints"],
}

KEYWORDS = {
    "food": ["recipe", "cook", "meal", "restaurant", "macaron", "food", "ingredient"],
    "pet_safety": ["lily", "lilies", "plant", "cat", "dog", "pet", "home"],
    "investing": ["buy", "sell", "stock", "portfolio", "shares", "position"],
    "kr_stock": ["samsung", "sk hynix", "kospi", "krx", "korean stock", "삼성", "하이닉스", "코스피"],
    "coding_repo": ["repo", "branch", "test", "deploy", "commit", "pr"],
    "security": ["token", "secret", "api key", "credential", "password"],
}


def classify_domains(query: str) -> list[str]:
    q = query.lower()
    domains = [domain for domain, words in KEYWORDS.items() if any(w in q for w in words)]
    return domains or ["general"]


def slots_for_domains(domains: Iterable[str]) -> list[str]:
    seen = set()
    slots = []
    for domain in domains:
        for slot in DOMAIN_SLOTS.get(domain, []):
            if slot not in seen:
                seen.add(slot)
                slots.append(slot)
    return slots
```

**Claude Opus acceptance for Task 1.1:**

- `DOMAIN_SLOTS` hardcoded copy is temporary only; production helper loads `memory-routing-policy.yaml`.
- `extends` is resolved recursively, so `kr_stock` includes investing slots.
- keyword fast path uses token-boundary regex, not raw substring matching (`cat` must not match `communicate`; `pr` must not match `approach`).
- Add regression test: `하이닉스 지금 어때?` routes to `kr_stock` and includes `holdings`/`average_cost`.

**Step 4: verify pass**

```bash
python -m pytest tests/agent/test_memory_routing.py -q
```

Expected: tests pass.

**Commit:**

```bash
git add agent/memory_routing.py tests/agent/test_memory_routing.py
git commit -m "feat: add memory routing domain slots"
```

---

### Task 1.1b: LLM open-set classifier schema 추가

**Objective:** 새 토픽이 나와도 `general`로 버리지 않고 domains/slots/new_topic_candidate를 JSON으로 받을 수 있는 schema와 parser를 만든다.

**Files:**
- Modify: `agent/memory_routing.py`
- Test: `tests/agent/test_memory_routing.py`

**Failing test:**

```python
from agent.memory_routing import parse_llm_route


def test_parse_llm_route_accepts_new_topic_candidate():
    route = parse_llm_route({
        "domains": ["home_lifestyle"],
        "slots": ["pets", "children"],
        "risk": "medium",
        "confidence": 0.72,
        "route_source": "llm",
        "reason": "Housewarming plant gifts may affect pets or children.",
        "new_topic_candidate": "home_gift_safety",
        "suggested_slots": ["recipient_household", "pets", "children"],
        "should_update_policy": True,
    })
    assert route.new_topic_candidate == "home_gift_safety"
    assert route.should_update_policy is True
    assert "pets" in route.slots
```

**Implementation sketch:**

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class MemoryRoute:
    domains: list[str]
    slots: list[str]
    risk: str
    confidence: float
    route_source: str
    reason: str
    new_topic_candidate: str | None = None
    suggested_slots: list[str] | None = None
    should_update_policy: bool = False


def parse_llm_route(payload: dict) -> MemoryRoute:
    domains = [str(x) for x in payload.get("domains", [])] or ["general"]
    slots = [str(x) for x in payload.get("slots", [])]
    suggested = [str(x) for x in payload.get("suggested_slots", [])]
    confidence = float(payload.get("confidence", 0.0))
    risk = str(payload.get("risk", "low"))
    return MemoryRoute(
        domains=domains,
        slots=slots,
        risk=risk,
        confidence=max(0.0, min(1.0, confidence)),
        route_source=str(payload.get("route_source", "llm")),
        reason=str(payload.get("reason", "")),
        new_topic_candidate=payload.get("new_topic_candidate"),
        suggested_slots=suggested,
        should_update_policy=bool(payload.get("should_update_policy", False)),
    )
```

**Verify:**

```bash
python -m pytest tests/agent/test_memory_routing.py -q
```

**Commit:**

```bash
git add agent/memory_routing.py tests/agent/test_memory_routing.py
git commit -m "feat: add open-set memory route schema"
```

---

### Task 1.1c: LLM classifier prompt and fallback policy 작성

**Objective:** LLM classifier가 stable JSON을 반환하도록 prompt/schema와 fallback 정책을 문서화하고 테스트한다.

**Files:**
- Modify: `agent/memory_routing.py`
- Test: `tests/agent/test_memory_routing.py`
- Modify: `docs/features/memory-routing/README.md`

**Prompt requirements:**

```text
Classify the user request for memory routing.
Return JSON only.
Do not answer the user task.
Prefer known domains when applicable.
If no known domain fits, include new_topic_candidate and suggested_slots.
High-risk domains should err on recall, but avoid injecting private facts unless relevant.
```

**Fallback policy:**

- LLM unavailable → deterministic safety trigger only
- invalid JSON → deterministic safety trigger only + debug event
- confidence < 0.45 and risk low/medium → do not add new slots
- confidence < 0.45 and risk high/critical → keep deterministic high-risk slots
- `should_update_policy=true` → write proposal artifact only, never mutate policy automatically

**Verify:**

```bash
python -m pytest tests/agent/test_memory_routing.py -q
git diff --check
```

**Commit:**

```bash
git add agent/memory_routing.py tests/agent/test_memory_routing.py docs/features/memory-routing/README.md
git commit -m "docs: define open-set memory classifier fallback policy"
```

---

### Task 1.1d: LLM classifier adapter with stubbed tests

**Objective:** open-set classifier가 실제 LLM 호출 없이도 테스트 가능한 adapter interface를 갖게 한다.

**Files:**
- Modify: `agent/memory_routing.py`
- Test: `tests/agent/test_memory_routing.py`

**API:**

```python
class RouteClassifier(Protocol):
    def classify(self, query: str, recent_turns: list[str] | None = None) -> MemoryRoute: ...
```

**Tests:**

- stub classifier returns `new_topic_candidate=home_gift_safety`
- invalid JSON falls back to deterministic safety route
- low confidence low-risk route does not add suggested slots
- high-risk deterministic slots survive LLM failure

**Verify:**

```bash
python -m pytest tests/agent/test_memory_routing.py -q
```

**Commit:**

```bash
git add agent/memory_routing.py tests/agent/test_memory_routing.py
git commit -m "feat: add memory route classifier adapter"
```

---

### Task 1.1e: policy learning proposal queue

**Objective:** 반복되는 `new_topic_candidate`를 자동 policy mutation이 아니라 reviewable proposal artifact로 저장한다.

**Files:**
- Create: `agent/memory_routing_proposals.py`
- Test: `tests/agent/test_memory_routing_proposals.py`
- Create directory at runtime: `docs/features/memory-routing/proposals/` or configured output path

**Behavior:**

- same `new_topic_candidate` increments count
- stores examples, suggested_slots, first_seen, last_seen
- never updates `memory-routing-policy.yaml` automatically
- ignores candidates derived only from untrusted tool/web content unless user query confirms the topic

**Verify:**

```bash
python -m pytest tests/agent/test_memory_routing_proposals.py -q
```

**Commit:**

```bash
git add agent/memory_routing_proposals.py tests/agent/test_memory_routing_proposals.py
git commit -m "feat: add memory routing proposal queue"
```

---

### Task 1.2: query rewrite helper 구현

**Objective:** provider `prefetch(query)`에 넘길 query를 deterministic safety trigger + LLM open-set route 기반으로 확장한다.

**Files:**
- Modify: `agent/memory_routing.py`
- Test: `tests/agent/test_memory_routing.py`

**Failing test:**

```python
from agent.memory_routing import rewrite_memory_query


def test_rewrite_food_query_adds_profile_slots():
    rewritten = rewrite_memory_query("Suggest a macaron recipe")
    assert "Original query: Suggest a macaron recipe" in rewritten
    assert "allergies" in rewritten
    assert "dietary_restrictions" in rewritten
```

**Implementation:**

```python
def rewrite_memory_query(query: str, route: MemoryRoute | None = None) -> str:
    route = route or build_memory_route(query)
    domains = route.domains
    slots = route.slots
    if not slots:
        return query
    return (
        f"Original query: {query}\n"
        f"Detected domains: {', '.join(domains)}\n"
        f"Relevant user profile slots to recall if available: {', '.join(slots)}"
    )
```

**Verify:**

```bash
python -m pytest tests/agent/test_memory_routing.py -q
```

**Commit:**

```bash
git add agent/memory_routing.py tests/agent/test_memory_routing.py
git commit -m "feat: add memory query rewrite helper"
```

---

### Task 1.3: benchmark evaluator script 작성

**Objective:** benchmark fixture의 expected behavior를 rule-based로 채점한다.

**Files:**
- Create: `tools/memory_routing_benchmark.py`
- Test: `tests/tools/test_memory_routing_benchmark.py`

**Failing test:**

```python
from tools.memory_routing_benchmark import score_answer


def test_score_answer_passes_must_include_and_must_not_include():
    expected = {
        "must_include": ["nut allergy", "avoid almond flour"],
        "must_not_include": ["almond flour as a safe ingredient"],
    }
    result = score_answer("Because of the nut allergy, avoid almond flour.", expected)
    assert result["passed"] is True


def test_score_answer_fails_missing_required_phrase():
    expected = {"must_include": ["toxic to cats"], "must_not_include": []}
    result = score_answer("Lilies are pretty flowers.", expected)
    assert result["passed"] is False
    assert "toxic to cats" in result["missing"]
```

**Implementation:**

```python
from __future__ import annotations


def score_answer(answer: str, expected: dict) -> dict:
    text = answer.lower()
    missing = [p for p in expected.get("must_include", []) if p.lower() not in text]
    forbidden = [p for p in expected.get("must_not_include", []) if p.lower() in text]
    return {
        "passed": not missing and not forbidden,
        "missing": missing,
        "forbidden": forbidden,
    }
```

**Verify:**

```bash
python -m pytest tests/tools/test_memory_routing_benchmark.py -q
```

**Commit:**

```bash
git add tools/memory_routing_benchmark.py tests/tools/test_memory_routing_benchmark.py
git commit -m "test: add memory routing benchmark scorer"
```

---

## 4. Phase 2 — Hermes integration

### Task 2.1: memory provider prefetch query rewrite 통합 위치 확인

**Objective:** core loop에서 external provider `prefetch(query)` 호출 직전 query rewrite를 적용할 위치를 찾는다.

**Files to inspect:**
- `agent/turn_context.py`
- `agent/memory_manager.py`
- `agent/context_engine.py`
- `agent/system_prompt.py`

**Commands:**

```bash
grep -RIn "prefetch(query)\|prefetch_all\|queue_prefetch\|memory provider" agent | head -80
python -m pytest tests/agent -q
```

**Expected:** identify single integration seam and baseline tests pass.

**No code change in this task unless seam is trivial.**

**Commit:** none or docs note commit.

---

### Task 2.2: rewrite 적용 flag 추가

**Objective:** config flag로 안전하게 query rewrite를 켜고 끌 수 있게 한다.

**Files:**
- Modify: config defaults file, likely `hermes_cli/config.py` or current config schema location
- Modify: `agent/memory_routing.py`
- Test: relevant config tests

**Config proposal:**

```yaml
memory:
  routing:
    enabled: false
    query_rewrite: true
    safety_gate: false
    debug: false
```

**Acceptance:**

- default off 또는 conservative on 여부를 PR에서 명시
- 기존 사용자에게 behavior breaking change 없어야 함
- tests pass

**Verify:**

```bash
python -m pytest tests/agent tests/hermes_cli -q
```

**Commit:**

```bash
git add <changed-files>
git commit -m "feat: add memory routing config"
```

---

### Task 2.3: external provider prefetch에 rewritten query 전달

**Objective:** memory routing enabled일 때 provider recall query를 확장한다.

**Files:**
- Modify: `agent/turn_context.py` or `agent/memory_manager.py`
- Modify: `agent/memory_routing.py`
- Test: `tests/agent/test_memory_manager.py` or new test

**Test idea:**

- fake memory provider records query passed to `prefetch()`
- user query: `Suggest a macaron recipe`
- expected provider query contains `allergies`

**Acceptance:**

- disabled config: original query preserved
- enabled config: rewritten query includes domain slots
- no provider schema changes required

**Verify:**

```bash
python -m pytest tests/agent/test_memory_routing.py tests/agent/test_memory_manager.py -q
python -m pytest tests/ -o 'addopts=' -q
```

**Commit:**

```bash
git add agent tests
git commit -m "feat: rewrite memory provider recall queries"
```

---

### Task 2.4: safety/profile gate MVP

**Objective:** high-risk domains에서 “확인해야 할 profile slots”를 runtime metadata로 생성한다.

**Files:**
- Modify: `agent/memory_routing.py`
- Test: `tests/agent/test_memory_routing.py`

**API proposal:**

```python
@dataclass(frozen=True)
class MemoryRoute:
    domains: list[str]
    slots: list[str]
    risk: str
    gate_required: bool
    rewritten_query: str
```

**Tests:**

```python
def test_food_route_requires_gate():
    route = build_memory_route("Suggest a macaron recipe")
    assert route.gate_required is True
    assert route.risk in {"high", "critical"}
    assert "allergies" in route.slots
```

**Acceptance:**

- route object can be logged/debugged
- no forced user-visible output yet
- later answer formatting can consume it

**Verify:**

```bash
python -m pytest tests/agent/test_memory_routing.py -q
```

**Commit:**

```bash
git add agent/memory_routing.py tests/agent/test_memory_routing.py
git commit -m "feat: add memory route safety metadata"
```

---

## 5. Phase 3 — Built-in memory curation

### Task 3.1: read-only curation report script

**Objective:** 현재 `MEMORY.md`/`USER.md`를 읽어 tier/domain/risk 후보를 제안한다. 실제 수정은 하지 않는다.

**Files:**
- Create: `tools/memory_curation_report.py`
- Test: `tests/tools/test_memory_curation_report.py`

**Behavior:**

Input:

```text
User has a nut allergy.
§
Project uses pytest.
```

Output:

```json
{
  "entries": [
    {"content": "User has a nut allergy.", "tier": "always", "domains": ["food", "health"], "risk": "critical"},
    {"content": "Project uses pytest.", "tier": "triggered", "domains": ["coding_repo"], "risk": "medium"}
  ],
  "usage": {"memory_chars": 2195, "user_chars": 1234},
  "recommendations": []
}
```

**Acceptance:**

- report only, no writes
- flags usage >80%
- never recommends deleting critical entries

**Verify:**

```bash
python -m pytest tests/tools/test_memory_curation_report.py -q
python tools/memory_curation_report.py --hermes-home ~/.hermes --format json > /tmp/memory-curation.json
python -m json.tool /tmp/memory-curation.json >/dev/null
```

**Commit:**

```bash
git add tools/memory_curation_report.py tests/tools/test_memory_curation_report.py
git commit -m "feat: add read-only memory curation report"
```

---

### Task 3.2: memory pressure dashboard docs

**Objective:** curation report를 어떻게 해석하고 운영할지 docs에 추가한다.

**Files:**
- Modify: `docs/features/memory-routing/README.md`
- Modify: `docs/features/memory-routing/PRD.md`

**Content:**

- usage thresholds:
  - <70% green
  - 70~85% yellow
  - >85% red
- delete policy:
  - critical: never auto delete
  - high: require explicit approval
  - medium/low: propose consolidation
- archive policy:
  - task logs → session_search
  - procedures → skills
  - user preferences → USER.md
  - environment conventions → MEMORY.md

**Verify:**

```bash
git diff --check
grep -R "critical: never auto delete" docs/features/memory-routing
```

**Commit:**

```bash
git add docs/features/memory-routing
git commit -m "docs: add memory pressure operating policy"
```

---

## 6. Phase 4 — Benchmark execution and expected reporting

### Task 4.0: isolated benchmark runner 구현

**Objective:** benchmark가 실제 Ryan `~/.hermes`를 오염시키지 않도록 임시 `HERMES_HOME`에서 memory 주입→질문 실행→응답 수집을 수행한다.

**Files:**
- Modify: `tools/memory_routing_benchmark.py`
- Test: `tests/tools/test_memory_routing_benchmark.py`

**Hard guard:**

```python
from pathlib import Path

def assert_isolated_hermes_home(hermes_home: Path) -> None:
    real = hermes_home.expanduser().resolve()
    forbidden = (Path.home() / ".hermes").resolve()
    if real == forbidden or forbidden in real.parents:
        raise RuntimeError("benchmark must not use real ~/.hermes")
```

**Runner requirements:**

- creates temp `HERMES_HOME` per run
- writes fixture memory into temp `MEMORY.md`/`USER.md` according to `memory_location`
- supports `--mode baseline|routed|open-set`
- runs each case 3 times and reports confidence intervals
- saves raw answers and judge results

**Verify:**

```bash
python -m pytest tests/tools/test_memory_routing_benchmark.py -q
```

**Commit:**

```bash
git add tools/memory_routing_benchmark.py tests/tools/test_memory_routing_benchmark.py
git commit -m "test: add isolated memory routing benchmark runner"
```

---

### Task 4.1: baseline run

**Objective:** routing disabled 상태의 indirect application baseline을 측정한다.

**Command shape:**

```bash
python tools/memory_routing_benchmark.py \
  --fixture docs/features/memory-routing/inmind-mini-benchmark.yaml \
  --mode baseline \
  --output /tmp/hermes-memory-routing-baseline.json
```

**Report format:**

```json
{
  "mode": "baseline",
  "cases": 20,
  "direct_recall_accuracy": 0.90,
  "indirect_application_accuracy": 0.15,
  "memory_hit_rate": 0.20,
  "safety_miss_rate": 0.55
}
```

**Acceptance:**

- No fabricated metrics; if model harness is unavailable, benchmark is incomplete and must not claim P0 metrics.
- Save raw outputs.

---

### Task 4.2: routed run

**Objective:** query rewrite + domain slots enabled 상태의 성능을 측정한다.

**Command shape:**

```bash
python tools/memory_routing_benchmark.py \
  --fixture docs/features/memory-routing/inmind-mini-benchmark.yaml \
  --mode routed \
  --output /tmp/hermes-memory-routing-routed.json
```

**Success target:**

- P0: indirect application >= 35%
- P1: indirect application >= 50%
- unseen topic useful routing >= 30% in P0, >=45% in P1
- safety miss rate baseline 대비 30% 감소

**Acceptance:**

- baseline/routed diff를 markdown table로 생성
- regression이면 feature flag remains off

---

### Task 4.3: open-set run

**Objective:** LLM open-set classifier + proposal queue enabled 상태에서 unseen-topic 성능을 측정한다.

**Command shape:**

```bash
python tools/memory_routing_benchmark.py \
  --fixture docs/features/memory-routing/inmind-mini-benchmark.yaml \
  --mode open-set \
  --runs-per-case 3 \
  --output /tmp/hermes-memory-routing-open-set.json
```

**Success target:**

- unseen topic useful routing >= 30% directional in P0
- confidence interval must be reported
- generated policy proposals are saved separately and not auto-applied

**Acceptance:**

- compare `baseline`, `routed`, and `open-set` by `memory_location` and `split`
- if open-set increases false positives materially, keep feature flag off

---

## 7. PR 분리 전략 / Parallel lanes

### Lane A — Docs and policy

- Branch: `docs/memory-routing-policy`
- Worktree: `/Users/ryan/sweep-wt/memory-routing-docs`
- Owns:
  - `docs/features/memory-routing/**`
- Excludes:
  - `agent/**`
  - `tools/**`
- Verify:
  - `git diff --check`
  - markdown link/file existence checks
- Commit:
  - `docs: add memory routing policy and benchmark spec`
- PR title:
  - `[hermes] Memory routing PRD and policy`

### Lane B — Pure routing helpers

- Branch: `feat/memory-routing-helpers`
- Worktree: `/Users/ryan/sweep-wt/memory-routing-helpers`
- Owns:
  - `agent/memory_routing.py`
  - `tests/agent/test_memory_routing.py`
- Excludes:
  - provider integration files
- Verify:
  - `python -m pytest tests/agent/test_memory_routing.py -q`
- Commit:
  - `feat: add memory routing helpers`
- Dependency:
  - Can run after Lane A or independently

### Lane C — Benchmark harness

- Branch: `test/memory-routing-benchmark`
- Worktree: `/Users/ryan/sweep-wt/memory-routing-benchmark`
- Owns:
  - `tools/memory_routing_benchmark.py`
  - `tests/tools/test_memory_routing_benchmark.py`
  - `tests/fixtures/memory_routing/**`
- Verify:
  - `python -m pytest tests/tools/test_memory_routing_benchmark.py -q`
- Commit:
  - `test: add memory routing benchmark harness`
- Dependency:
  - Needs fixture from Lane A or copies it into `tests/fixtures`

### Lane D — Provider integration

- Branch: `feat/memory-routing-prefetch`
- Worktree: `/Users/ryan/sweep-wt/memory-routing-prefetch`
- Owns:
  - `agent/memory_manager.py` or `agent/turn_context.py`
  - config defaults/schema
  - integration tests
- Verify:
  - targeted tests
  - full suite before merge
- Commit:
  - `feat: route memory provider prefetch queries`
- Dependency:
  - After Lane B

### Reconciliation note

If multiple lanes touch docs index or README, later lanes must rebase and preserve all links. Do not choose one side during conflict resolution; merge all lane references.

---

## 8. Rollout plan

### Stage 1: Docs-only

- PRD/plan/policy/benchmark fixture merged.
- No runtime behavior change.

### Stage 2: Feature flag off by default

- `agent.memory_routing` helpers merged.
- query rewrite available but disabled.

### Stage 3: Developer opt-in

- Enable on local profile or test profile.
- Run mini benchmark.
- Compare baseline vs routed.

### Stage 4: Conservative default

- If benchmark improves and false positives remain low, enable query rewrite for external provider prefetch only.
- Safety gate remains informational.

### Stage 5: Full memory curation loop

- Add read-only curation report.
- Add approval-based memory compaction workflow.

---

## 9. Verification checklist

Before final PR merge:

```bash
git status --short
git diff --check
python -m pytest tests/agent/test_memory_routing.py -q
python -m pytest tests/tools/test_memory_routing_benchmark.py -q
python -m pytest tests/ -o 'addopts=' -q
```

Manual checks:

- [ ] Built-in memory remains loaded as before.
- [ ] Routing disabled produces identical provider query.
- [ ] Routing enabled adds domain slots.
- [ ] No memory is deleted automatically.
- [ ] No cloud provider is required for MVP.
- [ ] Benchmark reports baseline and routed metrics separately.
- [ ] Docs state numbers are InMind-based expectations unless Hermes benchmark confirms them.

---

## 10. Immediate next action

1. Keep the generated artifacts at:

```text
/Users/ryan/outputs/hermes-memory-routing-inmind/PRD.md
/Users/ryan/outputs/hermes-memory-routing-inmind/plan.md
/Users/ryan/outputs/hermes-memory-routing-inmind/claude-opus-review.md
```

2. If implementation is requested, start with Lane A docs PR, then Lane B pure helper tests.

3. Do not mutate Ryan’s actual `MEMORY.md`/`USER.md` until a read-only curation report is produced and reviewed.
