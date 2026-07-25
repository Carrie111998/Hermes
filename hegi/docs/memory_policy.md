# Memory Safety Policy

HEGI의 일반 agent 계층은 Memory Forest read와 curator draft만 사용한다.
approve/commit은 모델 도구 목록에 등록하지 않으며, 인증된 Telegram 승인 이벤트를
처리하는 전용 approval worker만 private CLI 경계에서 호출한다. 이는 자율 Commit이
아니라 professor-authorized commit이다.

Draft 생성 조건은 모두 충족되어야 한다.

1. 설정된 교수 Telegram user ID가 승인한다.
2. 승인 문구가 지원하는 네 명령 중 하나다.
3. platform message ID가 이전에 처리되지 않았다.
4. 대상 meeting과 저장된 회의록이 존재한다.
5. Draft 직전에 Memory Forest를 다시 검색한다.
6. Draft payload의 제목·본문·schema가 유효하고 raw dict/JSON repr이 없다.
7. 높은 중복·충돌·불확실·검색 recall 경고가 없다.
8. commit 대상 Draft ID와 현재 승인 이벤트의 idempotency key가 연결된다.

`초안 만들어`는 pending Draft에서 멈춘다. `기억해`, `기억 승인해`,
`승인하고 저장해`는 같은 인증 이벤트 안에서 approve, commit, validate, audit,
index, backup까지 수행한다. 중단 시 `state.db`의 마지막 성공 단계부터 재개하며
commit 이후 재시작은 commit을 반복하지 않는다.

일반 대화 중 임의 approve/commit, 교수 승인 없는 실행, read-only MCP의 write 전환,
Memory Forest 파일 직접 쓰기는 계속 금지한다.
