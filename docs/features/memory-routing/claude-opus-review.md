# Claude Code Opus Review — Hermes Memory Routing PRD/Plan

실행:

```bash
zsh -lc 'cat /tmp/hermes-memory-routing-review-input.md | claude -p "Review the attached PRD.md and plan.md for Hermes memory routing..." --model opus'
```

## Review Result

# Hermes Memory Routing 리뷰

## 🔴 Must Fix (7)

**M1. §9.1 baseline은 단일 숫자로 성립하지 않는다 — bimodal이다**
- 근거: PRD §3.2 "Hermes built-in memory는 이미 Always-in-State" ↔ §9.1 "Indirect baseline 10~16%"
- 두 주장은 동시에 참일 수 없다. 사실이 3,575자(MEMORY 2,200 + USER 1,375) always 블록 **안**에 있으면 Hermes는 68.8% arm이고, 블록이 99% 포화라 **밖**으로 밀리면 4.8~16% arm이다. 즉 baseline은 "사실이 어디 있느냐"의 함수이고, 그래서 P0 목표 35%는 always-block 케이스에선 **개선이 아니라 회귀 목표**다.
- 수정: benchmark case에 `memory_location: always | overflow` 필드 추가 → 모든 지표를 이 축으로 stratify → §9.1 목표치는 **측정 후 재도출**.

**M2. Open-set classifier(FR2 layer 2·3)가 plan에 구현되지 않는다**
- PRD §12-5는 `baseline/routed/open-set` 3모드 측정을 요구하지만 plan Task 4.1·4.2에는 `--mode baseline`, `--mode routed`만 존재.
- policy learning queue artifact 생성 Task 없음.
- 수정: Task 1.1d(LLM client adapter + stubbed JSON 응답 테스트), Task 1.1e(`docs/features/memory-routing/proposals/*.json` 생성 + 중복 카운트), Task 4.3(`--mode open-set`) 신설.

**M3. Benchmark가 §9가 주장하는 것을 측정할 수 없다**
- substring scorer는 safety miss/false positive를 제대로 측정하지 못함.
- fixture가 영어 중심이라 한국어 실사용 실패 가능.
- runner 구현 Task가 없음.
- 수정: judged claims, ko/en 표현, control case, Task 4.0 runner, scorer-only 예외 삭제.

**M4. n=20은 P0 목표를 검증할 검정력이 없다**
- n=20/ unseen 5개로 30% 목표는 산술적으로 불안정.
- 수정: known ≥40 + unseen ≥20, case당 3회 실행, CI 병기.

**M5. plan대로 구현하면 import가 깨진다**
- `MemoryRoute` dataclass가 두 번 정의됨.
- `build_memory_route()` 정의 없음.
- substring keyword bug(`cat`, `pr`)와 `extends` 미해석.
- 수정: 단일 `MemoryRoute`, YAML loader + recursive extends, token-boundary regex, `rewrite_memory_query(query, route)`.

**M6. Benchmark가 실제 `~/.hermes`를 오염시킬 경로가 열려 있다**
- 수정: harness는 `HERMES_HOME=$(mktemp -d)` 강제, `assert hermes_home != ~/.hermes`.

**M7. 누락된 요구사항 2건 — write-path tier 부여 / classifier 비용**
- FR7 Memory Write Tagging 필요.
- NFR5 Routing Cost 필요.

## 🟡 Should Fix 요약

- `general` fallback 정의 명확화.
- triggered memory는 system prompt가 아닌 turn-local block에 주입.
- rewritten query에는 memory 값이 아니라 slot 이름만 포함.
- multi-turn context window 사용.
- supersedes/confirmed_absent 규칙 추가.
- 임계값 통일.
- In-context upper bound는 목표가 아니라 진단 지표.
- repo SSOT와 outputs snapshot 관계 명확화.

## 반영 상태

이 리뷰의 핵심 Must Fix/Should Fix는 2026-07-30 업데이트된 PRD.md/plan.md에 반영했다.
