# HEGI v2.0.2 — AI Research Secretary

HEGI는 여러 Hermes 프로필의 공진방 대화를 읽기 전용으로 수집해 연구회의 Episode,
한국어 구조화 회의록, Action Item, Memory Evaluation, Markdown/JSON 아카이브와
Telegram 보고로 변환한다.

회의록은 [작성·출력 정책](docs/minutes_policy.md)을 강제한다. 교수 발언은 교차 DB
중복 제거 후 `교수`로 정규화하며, 연구회의와 운영 장애를 분류해 서로 다른 템플릿을
사용한다. 출력 전 quality gate는 raw Python repr, 잘못된 화자명, Memory Forest 직접
쓰기 제안을 차단한다.

HEGI는 Hermes 핵심 agent loop나 tool schema를 수정하지 않는 독립 패키지다. LLM,
Telegram, MCP는 Hermes의 기존 호출 경로를 재사용한다. 일반 대화에는 Memory Forest
approve/commit을 노출하지 않는다. 설정된 교수 계정의 명시적 Telegram 승인 이벤트가
있을 때만 전용 worker가 professor-authorized commit을 수행한다.

`install.sh`은 현재 Hermes 환경에서 Memory Curator profile, Telegram group chat,
교수 user ID와 회의 참여 agent DB를 탐지한다. 비활성 예제 설정을 복사하거나 YAML을
수동 편집하지 않는다.

```bash
cd ~/.hermes/hermes-agent
hegi/scripts/install.sh
python -m hegi doctor
python -m hegi run-once
python -m hegi run-once --send
hegi/scripts/start.sh --send
python -m hegi status
```

교수가 HEGI 회의록에 `기억해` 또는 `초안 만들어`라고 답하면 profile-local
pre-dispatch plugin이 일반 Memory Curator 응답보다 먼저 메시지를 처리한다. 승인
작업은 SQLite에 단계별로 영속화된다. `초안 만들어`는 pending Draft에서 멈추고,
`기억해`는 fresh search부터 validate/audit/index/backup까지 한 번에 완료한다.

설치와 운영은 [operations.md](docs/operations.md), 안전 경계는
[memory_policy.md](docs/memory_policy.md)를 참고한다.
