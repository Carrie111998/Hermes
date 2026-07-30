# Hermes Memory Routing PRD

> **제품명:** Hermes Memory Routing & Always-in-State 개선
>
> **작성일:** 2026-07-30
>
> **배경 문서:** InMind: Keep It InMind — Benchmarking the Implicit-Association Blind Spot in Agent Memory, arXiv:2607.24368
>
> **대상:** Hermes Agent built-in memory, external memory providers, session_search, prompt assembly, memory curation workflow

> **Claude Code Opus 리뷰 반영:** 2026-07-30, `claude --model opus` 리뷰에서 지적된 baseline stratification, open-set implementation gap, benchmark validity, isolated HERMES_HOME, write-path tagging, routing cost 요구사항을 반영했다. 리뷰 전문은 `claude-opus-review.md`에 저장했다.

---

## 1. 요약

Hermes는 이미 `MEMORY.md` / `USER.md`를 세션 시작 시 system prompt에 주입하는 Always-in-State형 built-in memory와, `session_search`, external memory provider(Honcho, Mem0, Hindsight, Holographic, OpenViking, RetainDB, ByteRover, Supermemory)를 지원한다.

그러나 InMind 벤치마크가 보여준 핵심 문제는 단순 저장/검색 문제가 아니다.

> 에이전트가 사실을 저장하고 직접 질문에는 맞히더라도, 그 사실이 간접적으로 필요한 순간에는 검색하지 못한다.

예:

```text
저장된 사실: 사용자는 견과류 알러지가 있다.
사용자 질문: 마카롱 레시피 알려줘.
실패: 마카롱과 알러지 메모리가 의미적으로 가깝지 않아 검색 누락.
```

따라서 Hermes의 개선 방향은 “더 많은 memory backend 추가”가 아니라:

1. 어떤 기억을 항상 보이게 둘지 결정하는 **Memory Routing**
2. 질문 도메인에 따라 필요한 사용자 사실 유형을 떠올리는 **Domain Slot Recall**
3. 고위험 도메인에서 누락을 막는 **Safety/Profile Gate**
4. memory 포화 시 중요한 사실이 밀려나지 않게 하는 **Curation Policy**

를 추가하는 것이다.

---

## 2. 문제 정의

### 2.1 현재 Hermes memory 구조

공식 문서/GitHub 기준 확인 사항:

- Built-in memory
  - `~/.hermes/memories/MEMORY.md`: agent personal notes, 2,200 chars
  - `~/.hermes/memories/USER.md`: user profile, 1,375 chars
  - 세션 시작 시 system prompt에 frozen snapshot으로 주입
  - 세션 중 수정은 디스크에는 즉시 반영되지만 현재 prompt에는 다음 세션부터 반영

- Session search
  - SQLite + FTS5 기반 과거 세션 원문 검색
  - archive recall에는 적합
  - 질문과 의미적으로 먼 implicit association에는 취약

- External memory provider
  - 하나의 external provider만 active 가능
  - built-in memory는 external provider와 별도로 항상 active
  - provider lifecycle에는 `system_prompt_block()`, `prefetch(query)`, `queue_prefetch(query)`, `sync_turn()`, `on_session_end()`, `on_pre_compress()`, `on_memory_write()`가 있음

### 2.2 현재 한계

1. **Built-in memory 용량이 작고 수동 큐레이션 의존**
   - 현재 Ryan default profile의 personal memory는 99% 수준으로 포화 상태.
   - 중요한 사용자 사실과 구현 세부사항/환경 quirks가 섞이면 eviction 판단이 어려움.

2. **검색 query가 표면 질문에 종속**
   - “마카롱” 질문은 “견과류 알러지”를 직접 검색하지 않는다.
   - “백합” 질문은 “고양이를 키운다”를 직접 검색하지 않는다.

3. **memory 중요도 판단 기준이 명시적이지 않음**
   - 빈도/최근성/사용자 교정만으로는 “한 번 말했지만 치명적인 사실”을 보존하기 어렵다.

4. **고위험 도메인 gate가 일반화되어 있지 않음**
   - 음식/건강/투자/보안/법률/가족/반려동물 질문에서 어떤 profile fact를 확인해야 하는지 일관된 체크리스트가 없다.

---

## 3. 근거

### 3.1 InMind 벤치마크 수치

InMind 글/논문에서 제시된 대표 수치:

| 방식 | 간접 적용 성능 |
|---|---:|
| 검색 기반 memory systems | 약 4.8~16.0% |
| MemoryOS 최고치 | 14.4% |
| Naive RAG 최고치 | 16.0% |
| 200줄 profile Always-in-State | 68.8% |
| 직접 in-context 주입 upper bound | 84.0% |

계산:

- 68.8% / 16.0% = **4.3배**
- 68.8% / 14.4% = **약 4.78배**
- 68.8% - 16.0% = **+52.8%p**
- 84.0% - 14.4% = **+69.6%p**

해석:

> implicit association 문제에서는 “검색 성능 개선”보다 “중요 사실을 모델 시야 안에 두는 것”의 효과가 훨씬 크다.

### 3.2 Hermes 공식 구조와의 연결

Hermes built-in memory는 이미 Always-in-State이므로 InMind 해결 방향과 맞다.

하지만 현재 built-in memory는 plain text + strict char limit 구조라 다음 개선이 필요하다.

- critical fact 선별 정책
- domain/risk/tier 메타데이터
- 질문 도메인별 triggered memory 주입
- memory compaction/curation 기준
- provider prefetch query rewrite

### 3.3 Baseline stratification: always vs overflow

Claude Code Opus 리뷰 결과, Hermes의 baseline은 단일 숫자로 표현하면 안 된다. Hermes는 이미 built-in `MEMORY.md`/`USER.md`를 Always-in-State로 주입하므로, 필요한 사실이 always block 안에 있는 case와 memory overflow/archive에 있는 case는 서로 다른 성능 arm이다.

Benchmark case는 반드시 다음 필드를 가진다.

```yaml
memory_location: always | triggered | overflow | archive
```

모든 지표는 최소한 `memory_location=always`와 `memory_location=overflow/archive`로 stratify한다. InMind의 68.8% vs 14.4~16.0%는 cross-configuration 비교이며, 이미 always-in-state인 시스템에서 남은 theoretical headroom은 `84.0 - 68.8 = 15.2%p`로 본다. 반대로 필요한 사실이 overflow/archive에 있으면 10~16% arm으로 떨어질 수 있다.

따라서 §9의 정량 목표는 초기 benchmark 측정 전까지 확정 목표가 아니라 directional target으로 취급한다.

---

## 4. 목표

### 4.1 P0 목표

1. **Memory Tier Policy 정의**
   - `always`, `triggered`, `archive` 3단계로 memory를 분류.

2. **Critical Profile Curation**
   - `USER.md` / `MEMORY.md`에 들어갈 항목 기준을 정의.
   - task log, transient state, stale implementation details는 session_search/archive로 이동.

3. **Domain Slot Recall 설계**
   - 질문이 들어오면 표면 query가 아니라 필요한 사용자 사실 슬롯을 생성.
   - 예: food → allergies, diet, religion, household constraints.

4. **Safety/Profile Gate 설계**
   - 음식/건강/투자/보안/법률/반려동물 등 고위험 도메인에서 답변 전 memory slot 확인.

5. **측정 가능한 benchmark harness 설계**
   - InMind형 mini benchmark를 Hermes 환경에 맞게 작성.
   - direct recall / indirect application / in-context upper bound를 분리 측정.

### 4.2 P1 목표

1. **Provider-aware query rewrite**
   - external memory provider의 `prefetch(query)` 전 query를 domain slot 기반으로 확장.

2. **Memory pressure dashboard**
   - built-in memory 사용률, stale 후보, duplicate 후보, critical fact coverage 표시.

3. **Memory write reviewer 개선**
   - 새 memory 저장 시 tier/domain/risk 후보를 제안.

4. **Hermes docs/skill 반영**
   - memory 운영 skill 또는 docs에 routing policy 추가.

### 4.3 P2 목표

1. **Adaptive Always-in-State composer**
   - fixed `USER.md` / `MEMORY.md` 외에 세션 시작 또는 turn 시작 시 critical profile card 생성.

2. **InMind-style regression suite**
   - Hermes memory provider별 indirect application 성능을 주기적으로 비교.

3. **Learning loop 자동화**
   - 실패 케이스를 memory routing rule 또는 skill 개선 후보로 자동 전환.

---

## 5. 비목표

이번 PRD 범위에서 바로 하지 않는 것:

- Hermes memory backend 전체 리라이트
- 모든 memory provider 동시 활성화
- 사용자 private memory를 외부 cloud provider로 무조건 이전
- health/legal/financial 전문 판단 자동화
- LLM이 무승인으로 대량 memory 삭제
- InMind 전체 125문항 benchmark 완전 복제
- core prompt caching을 깨는 매 turn system prompt 대규모 변경

### 5.1 설계 원칙: 하드코딩은 안전망이지 제품 전체가 아니다

Keyword table은 MVP baseline, regression test, high-risk fast path에만 사용한다. 새 토픽을 `general`로 버리는 classifier는 InMind 문제가 지적한 blind spot을 반복하므로 허용하지 않는다.

제품 목표는 **rule-based safety trigger + LLM open-set classifier + policy learning loop**이다.

---

## 6. 사용자 스토리

### Story 1 — 음식/알러지

사용자가 “마카롱 레시피 알려줘”라고 묻는다. Hermes는 질문에 “알러지”라는 단어가 없어도 food domain slot을 활성화하고, 사용자 알러지 memory를 확인한 뒤 견과류 대체 재료를 제안한다.

**Acceptance Criteria**

- food domain 감지
- allergy/diet/religion slots 확인
- 관련 memory가 있으면 답변에 반영
- 관련 memory가 없으면 보수적 안내 포함

### Story 2 — 반려동물 안전

사용자가 “집에 백합 둬도 돼?”라고 묻는다. Hermes는 pet safety slot을 활성화하고, 사용자가 고양이를 키운다는 memory가 있으면 위험 경고를 우선 제공한다.

**Acceptance Criteria**

- plant/home/pet-safety domain 감지
- pet ownership slot 확인
- 고양이 memory가 있으면 백합 독성 경고

### Story 3 — 투자 답변

사용자가 특정 한국 주식 매수/매도 전략을 묻는다. Hermes는 투자 domain slot을 활성화하고, Ryan의 portfolio-aware 규칙, KR stock answer format, 보유종목 확인 원칙을 반영한다.

**Acceptance Criteria**

- investing/kr-stock domain 감지
- holdings/risk/action-card/style slots 확인
- 사용 skill 및 skipped chart skill 사유 명시
- 단순 일반론 대신 portfolio-aware 답변

### Story 4 — memory 포화 관리

memory 사용률이 80%를 넘으면 Hermes는 새 memory를 무작정 추가하지 않고, stale/duplicate/low-risk archive 후보와 critical fact 보존 후보를 함께 제안한다.

**Acceptance Criteria**

- usage threshold 감지
- consolidate/remove 후보 생성
- critical fact 삭제 방지
- user approval 또는 명시적 도구 호출 정책 준수

---

## 7. 기능 요구사항

### FR1. Memory Tier Model

Memory entry는 내부적으로 다음 필드를 가질 수 있어야 한다.

```yaml
content: string
tier: always | triggered | archive
domains: list[string]
risk: low | medium | high | critical
source: manual | auto_review | user_correction | tool_observation
confidence: low | medium | high
last_verified_at: date | null
```

초기 구현은 실제 `MEMORY.md` 포맷을 바꾸지 않고, sidecar JSON 또는 curation report에서만 관리해도 된다.

### FR2. Hybrid Open-set Domain Router

Domain classifier는 하드코딩된 keyword table로 고정하지 않는다. 실제 제품 구조는 다음 3계층으로 동작해야 한다.

1. **Deterministic Safety Triggers**
   - health, medication, security, investing, allergy, pet safety처럼 false negative 비용이 큰 domain은 rule 기반 fast path로 감지한다.
   - 이 레이어는 classifier 전체가 아니라 안전망이다.

2. **LLM Open-set Classifier**
   - known domain에 대한 JSON 분류를 수행한다.
   - 질문 표면어와 memory가 멀어도 필요한 사용자 profile slot을 추론한다.
   - 새 표현/새 상황을 `general`로 버리지 않고 `new_topic_candidate`와 `suggested_slots`를 생성한다.

3. **Policy Learning Loop**
   - 반복 등장하거나 실패를 만든 `new_topic_candidate`를 review queue에 올린다.
   - 승인 후 `memory-routing-policy.yaml`의 정식 domain/slot으로 승격한다.

초기 known domains:

- food
- health
- medication
- supplement
- pet_safety
- child_family
- travel
- investing
- kr_stock
- coding_repo
- security
- legal_admin
- home_lifestyle
- general

Classifier output schema:

```json
{
  "domains": ["food"],
  "slots": ["allergies", "dietary_restrictions"],
  "risk": "high",
  "confidence": 0.84,
  "route_source": "rule+llm",
  "reason": "Recipe request can be affected by allergies and dietary restrictions.",
  "new_topic_candidate": null,
  "suggested_slots": [],
  "should_update_policy": false
}
```

Unseen topic example:

```json
{
  "domains": ["home_lifestyle"],
  "slots": ["pets", "children", "household_constraints"],
  "risk": "medium",
  "confidence": 0.72,
  "route_source": "llm",
  "reason": "Housewarming plant gifts may interact with pets or children in the recipient household.",
  "new_topic_candidate": "home_gift_safety",
  "suggested_slots": ["recipient_household", "pets", "children", "allergies"],
  "should_update_policy": true
}
```

### FR3. Slot Generator

Slot generation은 policy lookup + LLM-proposed slots의 합집합으로 만든다. Known domain에서는 `memory-routing-policy.yaml`의 slots를 우선 사용하고, open-set 상황에서는 LLM이 제안한 slots를 confidence와 risk 기준으로 제한적으로 추가한다.

예:

```yaml
food:
  required_if_available:
    - allergies
    - dietary_restrictions
    - religion_food_rules
    - household_constraints

investing:
  required_if_available:
    - holdings
    - risk_tolerance
    - time_horizon
    - average_cost
    - answer_format_preferences

home_lifestyle:
  recommended_if_available:
    - pets
    - children
    - household_constraints
    - allergies
```

### FR4. Memory Retrieval Strategy

질문 처리 전 다음 순서로 memory context를 구성한다.

```text
1. Built-in Always-in-State: USER.md + MEMORY.md frozen snapshot
2. Domain Slot Recall: 질문 domain 기반 targeted memory lookup
3. Provider Prefetch: query rewrite 후 external provider prefetch
4. Session Search: 사용자가 과거 대화 참조 또는 구체적 회상 요청 시 사용
```

### FR5. Safety/Profile Gate

고위험 domain에서는 다음 중 하나를 수행한다.

- 관련 memory가 발견되면 답변에 반영
- memory가 없으면 “알려진 제약 정보 없음” 또는 “제약이 있으면 바뀔 수 있음”을 명시
- 고위험 조언은 단정 대신 확인/주의 표현 사용

### FR6. Benchmark Harness

Hermes용 mini benchmark를 만든다.

각 case는 다음 3문항을 가진다.

```yaml
memory: "User has a nut allergy."
direct_question: "What allergy does the user have?"
indirect_question: "Suggest a macaron recipe."
in_context_question: "Given the memory, suggest a macaron recipe."
expected_behavior: "Avoid almond flour and mention nut allergy."
```

측정 지표:

- Direct Recall Accuracy
- Indirect Application Accuracy
- In-context Upper Bound
- Memory Hit Rate
- Safety Miss Rate
- False Positive Personalization Rate

### FR7. Memory Write Tagging

Memory routing 품질은 write path에서 tier/domain/risk가 부여되어야 유지된다. 새 memory가 저장되거나 수정될 때 다음을 수행한다.

- `on_memory_write()` 또는 built-in `memory` tool write path에서 tier/domain/risk 후보를 생성한다.
- sidecar metadata key는 normalized content hash를 기본으로 한다.
- `MEMORY.md`/`USER.md` 수동 편집 또는 replace/remove로 hash orphan이 생기면 curation report에 표시한다.
- `supersedes` 필드로 오래된 memory와 대체 관계를 표현한다.
- `confirmed_absent`를 지원해 “알려진 제약 없음”과 “미확인”을 구분한다.
- critical/high risk memory는 자동 삭제하지 않고 proposal-only로 둔다.

```yaml
content_hash: sha256-normalized-text
tier: always
domains: [food, health]
risk: critical
supersedes: []
confirmed_absent: false
last_verified_at: 2026-07-30
```

---

## 8. 비기능 요구사항

### NFR1. Prompt caching 보존

- built-in memory snapshot은 세션 시작 시 frozen 유지.
- 매 turn 동적 context는 provider/prefetch block으로 제한.
- system prompt 대규모 재조립은 피한다.

### NFR2. Privacy

- health, religion, family, finance facts는 critical/private로 취급.
- cloud provider 사용 여부는 config/사용자 선택을 따른다.
- 기본 개선은 local built-in memory와 local reports 중심으로 가능해야 한다.

### NFR3. Safety

- memory deletion은 승인/검토 기반.
- critical fact는 자동 삭제하지 않는다.
- auto curation은 먼저 proposal artifact를 생성한다.

### NFR4. Observability

- 각 답변에서 memory routing이 영향을 준 경우 debug/evidence mode에서 확인 가능해야 한다.
- 최소한 benchmark에서는 어떤 slot이 활성화되었고 어떤 memory가 hit/miss 되었는지 기록한다.

### NFR5. Routing Cost and Latency

LLM open-set classifier는 매 turn 무조건 호출하지 않는다. 비용/지연 예산을 명시한다.

- deterministic safety trigger가 low-risk/general이고 최근 route cache가 유효하면 LLM classifier를 생략할 수 있다.
- p95 routing latency budget: 500ms 이하를 목표로 하되, 외부 LLM 호출 시 별도 측정한다.
- classifier model은 Haiku급/mini급 저비용 모델을 우선 사용한다.
- cache key는 normalized query + recent-turn topic signature + policy version으로 한다.
- budget 초과, LLM unavailable, invalid JSON이면 deterministic-only로 degrade한다.
- rewritten query에는 memory 값이 아니라 slot 이름만 포함한다. 외부 provider로 민감 memory value를 query rewrite 단계에서 보내지 않는다.

---

## 9. 성공 지표

### 9.0 Metric definitions

- **Unseen Topic Useful Routing Rate:** unseen case에서 정답 slot 집합 recall이 0.5 이상이고 false slot이 2개 이하인 비율.
- **False Positive Personalization Rate:** control case에서 필요 없는 개인 memory를 답변에 반영하거나 경고를 출력한 비율.
- **Safety Miss Rate:** high/critical case에서 필수 safety constraint를 누락한 비율.
- **Critical Fact Coverage @ fixed budget:** fixed char/token budget에서 critical/high tier memory가 보존되는 비율.

### 9.1 정량 목표

| Metric | Baseline | P0 | P1 | P2 | 비고 |
|---|---:|---:|---:|---:|---|
| Direct Recall Accuracy | 측정 | 유지 | 유지 | 유지 | `memory_location`별 stratify |
| Indirect Application Accuracy — always | 측정 | 측정 후 확정 | +5%p | +10%p | always block은 이미 68.8% arm일 수 있음 |
| Indirect Application Accuracy — overflow/archive | 측정 | 35%+ directional | 50%+ directional | 65%+ directional | InMind 검색형 10~16% arm 개선 목표 |
| Memory Hit Rate on indirect cases | 측정 | 40%+ directional | 60%+ | 70%+ | known/unseen split |
| Unseen Topic Useful Routing Rate | 측정 | 30%+ directional | 45%+ | 60%+ | n≥20, CI 병기 |
| Safety Miss Rate | 측정 | baseline 측정 | 30% 감소 | 50% 감소 | control case 포함 |
| False Positive Personalization Rate | 측정 | 측정 | 감소 | 감소 | 개인화가 불필요한 control case |
| Critical Fact Coverage @ fixed budget | 측정 | 90%+ | 95%+ | 95%+ | 단순 memory usage보다 우선 |
| In-context Upper Bound | 진단 | 진단 | 진단 | 진단 | 목표가 아니라 upper-bound diagnostic |

### 9.2 정성 목표

- 사용자가 “전에 말했잖아”라고 교정하는 빈도 감소
- 투자/건강/식단/반려동물 답변에서 개인화 누락 감소
- memory가 꽉 찼을 때 무작정 추가 실패가 아니라 curation proposal 생성
- memory와 skill의 책임 경계 명확화

---

## 10. 위험 및 대응

| 위험 | 설명 | 대응 |
|---|---|---|
| Over-personalization | 관련 없는 memory를 과도하게 적용 | domain/risk confidence, false positive metric |
| Privacy exposure | 민감 facts가 불필요하게 prompt에 노출 | critical/private tagging, cloud opt-in |
| Prompt bloat | triggered memory가 너무 커짐 | token budget, top-k, slot별 max chars |
| Stale memory | 오래된 사실이 계속 적용 | last_verified_at, review cadence |
| Deletion accident | 중요한 memory 자동 삭제 | proposal-only, approval required |
| Provider lock-in | 특정 memory provider에 종속 | provider-agnostic query rewrite / sidecar policy |

---

## 11. Open Questions

1. built-in memory plain text 포맷을 유지할 것인가, sidecar metadata를 둘 것인가?
2. hybrid classifier의 LLM 호출 조건과 cache TTL은 어떻게 둘 것인가?
3. Ryan default profile에서 memory curation을 먼저 실행할 것인가, benchmark harness부터 만들 것인가?
4. external provider는 현재 상태 유지인가, Honcho/Hindsight/Holographic 중 하나를 실험할 것인가?
5. 답변 본문에 memory routing evidence를 항상 표시할 것인가, debug/verbose일 때만 표시할 것인가?

---

## 12. 권장 MVP

MVP는 core code 수정 없이 다음으로 시작한다.

1. `memory-routing-policy.yaml` 작성
2. deterministic safety trigger와 LLM open-set classifier schema 작성
3. `memory_curation_report.py`로 built-in memory를 tier/domain/risk 후보로 분류
4. `inmind_mini_benchmark.yaml` 20문항 + unseen topic split 작성
5. `run_memory_routing_benchmark.py`로 baseline/routed/open-set 성능 측정
6. P0 결과를 바탕으로 Hermes core `prefetch(query)` 또는 prompt assembly 개선 PR 작성

이렇게 하면 위험을 낮추면서도 InMind식 문제를 수치로 검증할 수 있다.
