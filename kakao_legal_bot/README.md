# 모아 — 카카오톡 법률상담 AI 어시스턴트

변호사 상담방에서 1차 응대를 맡는 봇입니다. 호출되면 로컬 법률자료(RAG)와
국가법령정보 API를 뒤져 답하고, 문서 요청은 초안을 만들어 **변호사 검토를 거친 뒤**
상담자 이메일로 나갑니다.

```
    카카오톡                에뮬레이터(내 PC)              Railway (24시간)
 ┌───────────┐          ┌──────────────────┐        ┌──────────────────────┐
 │ 상담자     │ ──메시지→ │ KakaoTalk + Iris │ ─웹훅→ │ FastAPI /iris/webhook│
 │ 김변호사   │          │  (루팅 안드로이드) │        │  ├ 트리거 판정        │
 └───────────┘          └──────────────────┘        │  ├ RAG(sqlite FTS5)  │
       ↑                        ↑                    │  ├ 법령·판례 API      │
       └────── 답변 ────────────┘                    │  ├ LLM 도구 호출 루프  │
                 (직접 전송 or 릴레이 폴링)            │  └ 초안 → 변호사 검토  │
                                                     └──────────┬───────────┘
                                                                │ 승인 후
                                                          상담자 이메일(.docx)
```

---

## 1. 핵심 설계 세 가지

### ① 5초 룰 — 방이 침묵하지 않게

카카오 공식 챗봇(오픈빌더/상담톡) 콜백은 **5초 안에 응답이 없으면 실패** 처리됩니다.
법률 답변은 검색 + 생성이라 5초에 못 들어옵니다. 그래서 이렇게 짰습니다.

- 웹훅은 파싱·중복제거만 하고 **즉시 200 반환** (실측 4~11 ms)
- 답변은 태스크로 돌리고, `ACK_DEADLINE_MS`(기본 3.5초)와 **경주**시킵니다
  - 답변이 먼저 끝나면 → 그냥 답변만 보냄 (쓸데없는 "잠시만요" 없음)
  - 마감이 먼저 오면 → "찾아보는 중입니다" 먼저 보내고, 끝나면 답변을 이어 보냄
- `ANSWER_TIMEOUT_S`(기본 90초)를 넘겨도 **포기하지 않습니다.**
  "3분내로 답변드리겠습니다"라고 알리고 `ANSWER_EXTENSION_S`(기본 180초)만큼
  **하던 작업을 그대로 이어서** 진행합니다. 법령 API를 여러 번 왕복하는 질문은
  90초를 넘기는 게 정상이고, 거의 다 만든 답변을 버리고 사과하는 건 최악입니다.
- 총 한도(기본 270초)까지도 못 끝내면 그때 변호사에게 질문을 그대로 넘깁니다

> Iris 경로 자체에는 5초 강제가 없지만(웹훅과 전송이 분리돼 있음), 위 구조로 짜 두면
> 나중에 공식 상담톡 API로 갈아타도 그대로 통과합니다.

### ② 전송 경로 — 집 공유기 뚫지 않고

Iris는 내 PC 에뮬레이터에 있고 Railway는 클라우드에 있습니다. Iris → 서버(웹훅)는
잘 나가지만, **서버 → Iris는 NAT에 막힙니다.** 세 가지 모드를 지원합니다.

| `IRIS_SEND_MODE` | 동작 | 필요한 것 |
|---|---|---|
| `direct` | 서버가 Iris로 직접 POST | Cloudflare Tunnel / Tailscale / ngrok |
| `poll` | 서버는 큐에만 넣고 `relay/moa_relay.py`가 가져감 | 없음 (권장 시작점) |
| `hybrid` | direct 시도 → 실패하면 큐로 | 위 둘 중 아무거나 (기본값) |

### ③ 문서는 반드시 변호사를 거쳐서

모델이 쓴 초안은 `pending_review` 상태로 저장되고, 승인 전에는 **코드상 발송이 거부**됩니다
(`workflows.send_draft`). 변호사는 카톡에서 `/승인 12` / `/발송 12` 로,
또는 `/admin/drafts` 웹 화면에서 본문을 고쳐서 내보냅니다.

---

## 2. 준비물

| | 무엇 | 비고 |
|---|---|---|
| 1 | 루팅된 안드로이드 에뮬레이터 + KakaoTalk + [Iris](https://github.com/dolidolih/Iris) | 블루스택은 알림만 읽혀서 부족합니다 |
| 2 | 듀얼 번호 (통신사 3,000원) | **본 계정으로 돌리지 마세요.** 정지되면 본 계정이 날아갑니다 |
| 3 | LLM API 키 | Anthropic 또는 OpenAI |
| 4 | Railway 서버 + 볼륨 | 볼륨 없으면 재배포마다 기록이 사라집니다 |
| 5 | law.go.kr `OC` 아이디, data.go.kr 인증키 | 아래 4장 |

---

## 3. 설치

### 3-1. 서버 배포 (Railway)

1. 이 저장소를 Railway 프로젝트에 연결하고, 서비스의 **Root Directory 를
   `kakao_legal_bot` 으로** 지정합니다. (`Dockerfile` / `railway.json` 이 여기 있습니다.)
2. **Volume 을 `/data` 에 마운트**하고 변수 `DATA_DIR=/data` 를 넣습니다.
3. Variables 탭에 `.env.example` 의 키를 채웁니다. 최소한 이것들:

   ```
   ANTHROPIC_API_KEY   LLM_MODEL=claude-sonnet-5
   IRIS_WEBHOOK_SECRET OUTBOX_TOKEN  ADMIN_TOKEN   (전부 긴 랜덤 문자열)
   LAWYER_NAME  LAWYER_ROOM_ID  LAWYER_KAKAO_IDS
   PUBLIC_BASE_URL=https://<내서비스>.up.railway.app
   ```
4. 배포 후 `https://<주소>/health` 가 `"ok": true` 를 주는지 확인합니다.
   `missing_config` 에 뜬 항목은 아직 그 기능이 꺼져 있다는 뜻입니다.

**Replica 는 1개로 두세요.** 저장소가 SQLite라 여러 인스턴스가 같이 쓰면 깨집니다.

로컬 실행도 같습니다:

```bash
pip install -r kakao_legal_bot/requirements.txt
cp kakao_legal_bot/.env.example .env && vi .env
uvicorn kakao_legal_bot.app.main:app --reload --port 8000
```

### 3-2. Iris 쪽 설정

1. 에뮬레이터에 카카오톡 설치 → **듀얼 번호로 새 계정 가입** → 상담용 계정으로 로그인
2. Iris 설치 후 웹훅 주소를 서버로 지정:
   `https://<내서비스>.up.railway.app/iris/webhook`
3. 헤더 `X-Iris-Secret: <IRIS_WEBHOOK_SECRET>` 를 넣을 수 있으면 넣으세요.
   못 넣는 버전이면 쿼리로도 받습니다: `...?secret=<값>`
   (그것도 어려우면 `IRIS_WEBHOOK_SECRET` 을 비워 두면 검증을 건너뜁니다.)

### 3-3. 릴레이 (poll / hybrid 모드일 때)

에뮬레이터가 도는 PC에서 같이 띄웁니다. 파이썬 표준 라이브러리만 씁니다.

```bash
python kakao_legal_bot/relay/moa_relay.py \
  --server https://<내서비스>.up.railway.app \
  --token  "$OUTBOX_TOKEN" \
  --iris   http://127.0.0.1:3000
```

`direct` 로 갈 거라면 대신 Cloudflare Tunnel 등으로 Iris(3000 포트)를 공개하고
`IRIS_BASE_URL` 을 그 주소로 두면 릴레이는 필요 없습니다.

### 3-4. 로컬 법률자료 넣기 (RAG)

`corpus/` 에 주석서·법률서적·사무실 서면을 넣고 색인합니다.
`.txt .md .docx .pdf .json .jsonl` 를 읽습니다. (PDF는 `pip install pypdf` 필요)

```bash
python -m kakao_legal_bot.app.rag.ingest ./corpus
python -m kakao_legal_bot.app.rag.ingest --search "임대차 보증금 반환"   # 검색 테스트
python -m kakao_legal_bot.app.rag.ingest --stats
```

조문처럼 구조가 있는 자료는 JSONL이 제일 깔끔합니다:

```json
{"title": "민법", "locator": "제618조", "text": "임대차는 당사자 일방이 …"}
```

한국어 검색은 **음절 바이그램 색인**을 씁니다. SQLite 기본 토크나이저는
`임대차보증금반환청구`를 한 덩어리로 봐서 "보증금 반환"으로는 안 걸리는데,
바이그램으로 쪼개 넣으면 부분 일치가 BM25 랭킹과 함께 동작합니다. 형태소 분석기
(mecab 등) 설치가 필요 없어 Railway에서 그냥 뜹니다.

임베딩까지 쓰려면 `RAG_EMBEDDINGS=true`, `OPENAI_API_KEY` 를 넣고
`python -m kakao_legal_bot.app.rag.ingest ./corpus --embed`.

---

## 4. 법령·판례 API 키

### law.go.kr (`LAW_OC`)

[open.law.go.kr](https://open.law.go.kr) 에서 OPEN API를 신청하면, 등록한 이메일의
**@ 앞부분**이 그대로 `OC` 값이 됩니다. (예: `kjccjk@gmail.com` → `LAW_OC=kjccjk`)
이 하나로 아래가 전부 열립니다.

| 도구 | target | 쓰임 |
|---|---|---|
| `search_law` / `get_law_text` | `law` | 현행법령 목록·본문 |
| `search_precedent` / `get_precedent` | `prec` | 판례 목록 → 일련번호 → 본문 |
| `search_ordinance` | `ordin` | 자치법규(조례·규칙) |
| `search_admin_rule` | `admrul` | 행정규칙(훈령·예규·고시) |
| `search_legal_forms` | `licbyl` | 별표·서식 |
| `search_constitutional_decision` | `detc` | 헌재결정례 |

### data.go.kr (`DATA_GO_KR_KEY`)

헌법재판소 판례정보(`9750000/PrecedentInfomationService`), 법제처 목록 API
(`1170000/law`), 생활법령(easylaw SOAP)에 씁니다. **디코딩 키**를 넣으세요.
인코딩 키(`%2F` 가 보이는 것)를 넣어도 서버가 한 번 디코딩해서 씁니다 —
이중 인코딩으로 인증이 깨지는 건 이 API에서 제일 흔한 삽질입니다.

응답은 전부 SQLite에 캐시됩니다(`LAW_CACHE_TTL_S`, 기본 24시간). 일일 호출 한도가
있는 API라 같은 질문이 반복돼도 한 번만 나갑니다.

> ⚠️ 키를 문서·메신저·이슈에 붙여넣지 마세요. 이미 노출된 키는
> 포털에서 **재발급**하고 이 저장소에는 절대 커밋하지 마세요. (`.env` 는 커밋 금지)

### MCP 서버로도 씁니다

같은 검색을 Claude Code / Hermes 에서 쓰려면:

```bash
claude mcp add korean-law -- python kakao_legal_bot/mcp_law_server.py
```

`LAW_OC`, `DATA_GO_KR_KEY` 를 환경변수로 넘기면 됩니다. 의존성 없이 JSON-RPC를
직접 말하므로 MCP SDK 설치가 필요 없습니다.

---

## 5. 방에서 쓰는 법

### 호출 규칙

- **단체방**: `모아` 라고 불러야 답합니다. (`BOT_ALIASES` 로 별칭 추가)
- **1:1 상담방**: 부르지 않아도 답합니다. Iris가 1:1임을 알려주면 자동 인식하고,
  아니면 변호사가 그 방에서 `/자동` 한 번 쳐서 등록합니다.
- 변호사가 `/개입` 하면 모아는 부를 때만 답합니다. `/조용` 이면 완전 정지.
- 자기 메시지, 사진·이모티콘, 무시 대상 발신자는 애초에 토큰을 쓰지 않습니다.

### 상담자용

| 명령 | 뜻 |
|---|---|
| `/도움말` | 사용법 |
| `/이메일 hong@example.com` | 문서 받을 주소 등록 |
| `/변호사` | 변호사에게 즉시 연결 |

### 변호사용 (`LAWYER_KAKAO_IDS` 또는 `LAWYER_ROOM_ID` 에서만 동작)

| 명령 | 뜻 |
|---|---|
| `/개입` · `/복귀` | 이 방에서 모아를 호출형으로 전환 / 해제 |
| `/조용` · `/재개` | 이 방 완전 정지 / 해제 |
| `/자동` · `/수동` | 이 방을 1:1 상담방으로 등록 / 해제 |
| `/초안` | 검토 대기 초안 목록 |
| `/승인 12` | 12번 초안 승인 |
| `/발송 12` | 12번 초안을 상담자 이메일로 발송 |
| `/상태` | 모델·자료·큐 상태 |

### 초안 검토 화면

`https://<내서비스>/admin/drafts?token=<ADMIN_TOKEN>`
제목·본문·이메일·메모를 고치고 **저장 → 승인 → 발송**. 발송하면 `.docx` 첨부로
상담자에게 나가고, 상담방에도 "보내드렸습니다" 안내가 자동으로 올라갑니다.

### 성격·규칙 바꾸기

`persona.md` 한 파일이 전부입니다. 금지 조항은 셋만 넣어 뒀습니다
(개인정보 / 변호사 아님 / 위법 조력 금지). 금지 목록을 늘릴수록 답변이 방어적으로
납작해지니, 늘리고 싶으면 한 줄씩 넣고 실제 답변을 보면서 조정하세요.

---

## 6. 카톡방을 상담자마다 자동으로 만드는 문제

원하시는 그림은 "채널로 신청 → 신청자별 방 자동 생성 → 봇 자동 참여" 인데,
**여기엔 카카오 쪽 제약이 하나 있습니다.**

- 카카오톡 채널의 상담은 *채널 상담톡* 이라는 별도 채널이고, 일반 카톡 채팅방이
  아닙니다. Iris는 **일반 카톡방만** 읽고 씁니다.
- Iris로 **방을 새로 만들 수는 없습니다.** 기존 방에 읽고 쓰는 도구입니다.

실무적으로 되는 조합은 이렇습니다.

1. **오픈채팅 1:1 방식 (권장)** — 채널 자동응답 메시지에 *1:1 오픈채팅 링크*를 넣습니다.
   신청자가 링크를 누르면 신청자별 1:1 방이 자동으로 생깁니다. 그 방은 봇 계정이
   이미 들어가 있는 상태라, 첫 메시지가 그대로 웹훅으로 들어옵니다.
   → 이 코드가 그대로 동작합니다. 첫 메시지에 자동으로 인사말이 나갑니다.
2. **채널 상담톡 API** — 카카오 비즈니스 정식 경로. 방 관리가 깔끔하지만 심사와
   비용이 있고 Iris 대신 상담톡 API 연동을 새로 붙여야 합니다. (5초 룰이 진짜로
   적용되는 건 이쪽입니다 — 그래서 위 ①처럼 짜뒀습니다.)

`corpus`, 명령어, 초안 흐름은 어느 쪽이든 그대로 재사용됩니다.

---

## 7. 알아두실 위험 (숨기지 않고 적습니다)

- **카카오 이용약관.** 비공식 클라이언트 제어(에뮬레이터 + Iris)는 카카오 약관 위반
  소지가 있고, 계정이 영구 정지될 수 있습니다. 그래서 듀얼 번호 별도 계정을 쓰는 겁니다.
  본 계정·사무실 대표 계정으로는 돌리지 마세요.
- **변호사법.** 봇의 답변이 "법률사무 처리"로 보이지 않도록, 최종 판단은 변호사
  검토를 거치는 구조로 짜뒀습니다(문서는 승인 없이 못 나감). 광고·수임 표현은
  변협 규정 확인이 필요합니다.
- **개인정보보호법.** 상담 내용은 민감정보가 될 수 있습니다. 기본값으로
  방별 최근 24턴만 보관하고, 발신자 식별자는 해시(`PSEUDONYM_SALT`)로 저장하며,
  90일 뒤 자동 삭제합니다(`HISTORY_RETENTION_DAYS`). 표시이름 원본을 남기려면
  `STORE_RAW_SENDER=true` 지만, 굳이 켜지 마시길 권합니다.
- **모델 오답.** 조문·판례는 도구로 확인한 것만 인용하도록 프롬프트를 걸었지만
  100%는 아닙니다. `answers` 테이블에 질문·답변·인용·사용도구가 전부 남으니
  주기적으로 훑어보세요.

---

## 8. 개발

```bash
python -m pytest tests/kakao_legal_bot -q      # 145개
```

네트워크는 전부 목입니다. `test_end_to_end.py` 는 LLM 엔드포인트와 Iris만
가짜로 두고 나머지(트리거·저장·도구 루프·법령 클라이언트·5초 경주·메시지 분할·감사
로그)는 실제 코드를 통과시킵니다.

| 파일 | 역할 |
|---|---|
| `app/main.py` | 웹훅 / 아웃박스 / 헬스체크 |
| `app/pipeline.py` | 5초 경주, 인사말, 레이트리밋, 후속작업 |
| `app/trigger.py` | 호출 판정 (순수 함수, 네트워크 없음) |
| `app/agent.py` · `app/llm.py` · `app/tools.py` | 프롬프트 · 도구 루프 · 도구 정의 |
| `app/rag/` | 바이그램 색인, 인제스트 |
| `app/lawapi/` | 법령·판례 API 클라이언트 |
| `app/workflows.py` · `app/admin.py` | 초안 생성 · 변호사 검토 · 이메일 |
| `relay/moa_relay.py` | 에뮬레이터 옆에서 도는 아웃박스 릴레이 |
| `mcp_law_server.py` | 법령 검색 MCP 서버 |
