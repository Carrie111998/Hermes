#!/usr/bin/env python3
"""Codex draft worker — run this on the lawyer's own PC.

Why it exists: a document draft is the one job where nobody is waiting.
The client already has their answer; the draft goes to the lawyer for review
before it reaches anyone. So it can be produced anywhere — including on the
machine that already holds the ChatGPT subscription.

That buys three things at once: Codex's agentic quality (it goes and finds
the statutes and checks itself), no API cost for drafts, and no expiring
subscription credential sitting on an unattended server.

    python moa_draft_worker.py --server https://moa.example.com \\
                              --token "$DRAFT_WORKER_TOKEN"

Requires the `codex` CLI on PATH, already signed in (`codex login`).
Standard library only — nothing to install alongside it.

If the PC is switched off, jobs simply wait in the server's queue.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PROMPT = """당신은 대한민국 변호사 사무실의 문서 작성 담당입니다.
아래 상담 내용을 바탕으로 [{kind}] 초안을 작성하세요.

작성 규칙:
- 실제 문서 서식 그대로. 제목·당사자·본문·날짜·서명란까지 포함합니다.
- 근거 법령과 판례는 반드시 확인한 것만 인용하세요. 확인이 안 되면 인용하지 마세요.
- 사실관계가 비어 있는 자리는 [ ] 로 표시해 변호사가 채우게 하세요. 지어내지 마세요.
- 문서 문어체로 씁니다. 인사말·설명·머리말을 덧붙이지 마세요.
- 완성된 문서 본문만 {outfile} 파일에 저장하세요. 그 밖의 파일은 만들지 마세요.

[문서 제목]
{title}

[작성 지시]
{instructions}

[상담 대화 기록]
{transcript}
"""


def _request(url: str, token: str, payload: dict | None, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"X-Worker-Token": token, **({"Content-Type": "application/json"} if data else {})},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        body = response.read().decode("utf-8", errors="replace")
    return json.loads(body) if body.strip().startswith(("{", "[")) else {}


def run_codex(job: dict, codex_bin: str, timeout: float, model: str) -> str:
    """Run one job through `codex exec` and return the document text."""
    workdir = Path(tempfile.mkdtemp(prefix="moa-draft-"))
    outfile = workdir / "draft.md"
    prompt = PROMPT.format(
        kind=job.get("kind") or "법률문서",
        title=job.get("title") or "법률문서 초안",
        instructions=job.get("instructions") or "",
        transcript=job.get("transcript") or "",
        outfile=outfile.name,
    )
    command = [codex_bin, "exec", "--cd", str(workdir)]
    if model:
        command += ["--model", model]
    command.append(prompt)

    try:
        completed = subprocess.run(  # noqa: S603
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(workdir),
        )
        if outfile.exists():
            text = outfile.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text
        # Codex sometimes answers inline instead of writing the file; the
        # transcript is still a usable draft, so don't throw the work away.
        stdout = (completed.stdout or "").strip()
        if completed.returncode != 0 and not stdout:
            raise RuntimeError(
                f"codex exited {completed.returncode}: {(completed.stderr or '')[:300]}"
            )
        if not stdout:
            raise RuntimeError("codex produced no document")
        return stdout
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="모아 초안 작성 워커 (Codex)")
    parser.add_argument("--server", required=True, help="예: https://moa.example.com")
    parser.add_argument("--token", required=True, help="서버의 DRAFT_WORKER_TOKEN 과 같은 값")
    parser.add_argument("--codex", default="codex", help="codex 실행 파일 경로")
    parser.add_argument("--model", default="", help="codex --model 에 넘길 값 (선택)")
    parser.add_argument("--interval", type=float, default=20.0, help="큐 확인 간격(초)")
    parser.add_argument("--job-timeout", type=float, default=1500.0, help="한 건 제한 시간(초)")
    parser.add_argument("--http-timeout", type=float, default=30.0)
    parser.add_argument("--once", action="store_true", help="한 번만 확인하고 종료")
    args = parser.parse_args(argv)

    server = args.server.rstrip("/")
    if shutil.which(args.codex) is None:
        print(f"[draft-worker] codex 를 찾을 수 없습니다: {args.codex}", file=sys.stderr)
        return 1

    print(f"[draft-worker] {server} 감시 시작 ({args.interval:.0f}초 간격)", flush=True)
    backoff = args.interval
    while True:
        try:
            data = _request(f"{server}/drafts/queue?limit=1", args.token, None, args.http_timeout)
            jobs = data.get("jobs") or []
            backoff = args.interval
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            backoff = min(backoff * 2, 300.0)
            print(f"[draft-worker] 큐 조회 실패: {exc} ({backoff:.0f}초 후 재시도)",
                  file=sys.stderr, flush=True)
            if args.once:
                return 1
            time.sleep(backoff)
            continue

        if not jobs:
            if args.once:
                print("[draft-worker] 대기 중인 초안이 없습니다", flush=True)
                return 0
            time.sleep(args.interval)
            continue

        for job in jobs:
            job_id = job["id"]
            label = f"#{job_id} {job.get('kind', '')} · {job.get('title', '')}"
            print(f"[draft-worker] 작성 시작 {label}", flush=True)
            started = time.monotonic()
            try:
                body = run_codex(job, args.codex, args.job_timeout, args.model)
            except (subprocess.TimeoutExpired, RuntimeError, OSError) as exc:
                print(f"[draft-worker] 실패 {label}: {exc}", file=sys.stderr, flush=True)
                try:
                    _request(
                        f"{server}/drafts/{job_id}/fail",
                        args.token,
                        {"error": str(exc)},
                        args.http_timeout,
                    )
                except (urllib.error.URLError, TimeoutError, ValueError) as report_exc:
                    print(f"[draft-worker] 실패 보고 실패: {report_exc}", file=sys.stderr, flush=True)
                continue

            try:
                _request(
                    f"{server}/drafts/{job_id}/result",
                    args.token,
                    {"body": body},
                    args.http_timeout,
                )
                elapsed = time.monotonic() - started
                print(f"[draft-worker] 완료 {label} ({elapsed:.0f}초, {len(body)}자)", flush=True)
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                # The document exists but could not be delivered. Keep it so
                # the work is not lost, and let the server's stale-job sweep
                # put the request back in the queue.
                fallback = Path.home() / f"moa-draft-{job_id}.md"
                fallback.write_text(body, encoding="utf-8")
                print(
                    f"[draft-worker] 전송 실패 {label}: {exc}\n"
                    f"               초안을 {fallback} 에 저장했습니다",
                    file=sys.stderr,
                    flush=True,
                )

        if args.once:
            return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[draft-worker] 종료", flush=True)
