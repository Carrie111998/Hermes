# corpus/

여기에 모아가 참고할 자료를 넣습니다. 이 디렉터리의 내용물은 `.gitignore` 로
제외되어 있습니다 — 주석서·서적·사건 서면은 저장소에 올리지 마세요.

읽는 형식: `.txt` `.md` `.docx` `.pdf` `.json` `.jsonl`

```bash
python -m kakao_legal_bot.app.rag.ingest ./corpus
```

폴더 구조는 자유입니다. 파일명이 그대로 인용 출처로 표시되니
`민법주해_임대차.md` 처럼 알아볼 수 있게 지어두면 답변의 각주가 읽기 좋아집니다.

조문·판례처럼 항목이 나뉘는 자료는 JSONL 이 가장 정확합니다:

```json
{"title": "민법", "locator": "제618조", "text": "임대차는 당사자 일방이 …"}
{"title": "민법", "locator": "제623조", "text": "임대인은 목적물을 임차인에게 …"}
```
