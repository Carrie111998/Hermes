# 법률 위키 볼트 — 에이전트 규칙 (스키마 층)

이 파일은 이 볼트를 관리하는 LLM 에이전트(코덱스 등)의 작업 규칙입니다.
LLM WIKI 3층 구조에서 **스키마 층**에 해당합니다. 사람이 소스를 고르고
질문하며, 에이전트가 요약·상호참조·정리·유지보수를 전담합니다.

## 층 구조

```
vault/
  raw/          ← 원문. 불변. 절대 수정하지 않는다 (읽기만).
    판례/  법령/  주석서/  서적/  상담사례/   (casefiles 에서 가져옴)
  wiki/         ← 에이전트가 쓰는 노트. 여기만 수정한다.
  hubs/         ← 자동 생성 허브노트 (build hubs). 직접 고치지 않는다.
  index.md      ← 내용 목차 — 모든 노트의 한 줄 요약 (ingest 때마다 갱신)
  log.md        ← 시간순 작업 일지 (append-only)
  AGENTS.md     ← 이 파일
```

## 노트 규칙 (frontmatter)

- `kind`: 판례 | 법령 | 주석서 | 서적 | 실무편람 | 서식 | 상담사례
- 날짜는 반드시: 판례 `decided_on`(선고일) · 법령 `effective_on`(시행일) ·
  나머지 `written_on`(작성·발행일). 전부 YYYY-MM-DD.
- 조문·판례·중요 키워드는 `statutes` / `cases` / `keywords` 와 본문
  `[[링크]]` 로. 표기는 조 단위 통일: 민28, 민법 제28조 → **[[민법 제28조]]**.
- 개정 전 조문 노트는 지우지 않는다 — `superseded_by` 로 연혁 처리
  (`build lint --apply` 가 날짜 비교로 자동 표시).

## 작업 흐름 (운영)

**Ingest** — 새 원문이 raw/ 에 오면:
1. `python -m kakao_legal_bot.app.wiki.build stub` → 뼈대와 `_wiki-jobs.jsonl`
2. 작업열의 각 항목마다 `wiki_prompt.md` 규칙대로 본문을 쓴다.
   상담사례 항목(`kind: consult_case`)은
   `python -m kakao_legal_bot.app.casefile prompt` 의 규칙을 따른다 —
   **가명화가 최우선**이다.
3. `build index` → `build hubs` 로 그래프와 허브 갱신.
4. index.md 에 새 노트의 한 줄 요약을 추가하고, log.md 에
   `## [YYYY-MM-DD] ingest | 제목` 한 줄을 append.

**Query** — 질문을 받으면 index.md → 관련 노트 → `build related` 순으로
넓힌다. 좋은 답(비교표·분석)은 노트로 만들어 wiki/ 에 다시 넣는다 —
탐구도 축적되어야 한다. log.md 에 `## [날짜] query | 질문` 기록.

**Lint** — 주기적으로 `build lint --apply`. 날짜로 판정되는 것(개정 전
조문, 낡은 발행본)은 자동 반영되고, 내용 모순은 `_lint-worklist.jsonl` 에
남는다. **내용 판단은 자동으로 하지 않는다** — 워크리스트를 읽고 최신
법령·판례를 근거로 직접 고친 뒤, 근거를 노트에 남긴다.

**Sync** — `python -m kakao_legal_bot.app.wiki.sync daily` 가 개정 법령과
최근 판례를 raw 로 가져오고 영향받는 기존 노트를 `_sync-worklist.jsonl`
에 적는다. 그 목록의 노트를 최신 기준으로 수정한다.

**상담사례 (분야별 축적)** — 서버의 `DATA_DIR/casefiles/` 에서 새 사건파일을
raw/상담사례/ 로 가져온 뒤 ingest 한다. 같은 분야 사례가 3건 이상이면
"분야 총설" 노트(예: [[사해행위취소]] 총설)를 만들어 사례를 링크로 묶고
공통 법리·질문 체크리스트를 정리한다. 이 총설이 상담 AI 의 참고자료가
되므로, 실명·연락처가 한 글자라도 남아 있으면 안 된다.

## 금지

- raw/ 수정, hubs/ 직접 수정, 근거 없는 조문·판례 인용, 가명화 생략,
  개정 전 자료 삭제(연혁으로 남길 것), 두 노트의 모순을 날짜 근거 없이
  임의로 한쪽 편들기.
