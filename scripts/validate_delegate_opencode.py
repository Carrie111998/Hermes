#!/usr/bin/env python
"""Live e2e validation of the OpenCode delegate_session backend (plan U4).

Proves, against a real local ``opencode serve`` (no mocks):

1. ``delegate_session(action='start', backend='opencode')`` opens a session.
2. A delegate goal that forces the OpenCode ``ask`` tool produces a
   ``question.asked`` request.
3. Hermes answers it autonomously via ``_auto_answer_delegate_question`` /
   ``run_oneshot`` (a parent-agent stub pins the expected answer in its
   context) — zero user interaction.
4. The OpenCode session transcript contains the Hermes-generated reply and
   the delegate's final report.

Usage: python scripts/validate_delegate_opencode.py [workdir]
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def log(step: str, detail: str = "") -> None:
    print(f"[validate] {step}{': ' + detail if detail else ''}", flush=True)


class ParentStub:
    """Minimal parent-agent stand-in: carries context run_oneshot reads."""

    def __init__(self, secret: str):
        self.session_id = "validate-opencode-e2e"
        self.model = ""
        self.provider = ""
        self.base_url = ""
        self.api_key = ""
        self.api_mode = ""
        self.auth_mode = ""
        self._session_messages = [
            {
                "role": "user",
                "content": (
                    "Project decision already made: the password for this task is "
                    f"exactly '{secret}'. Any delegate that asks for the password "
                    "must be told this value verbatim."
                ),
            },
            {"role": "assistant", "content": f"Understood; the password is {secret}."},
        ]


def main() -> int:
    secret = "sunset-ferret-42"
    workdir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="oc-e2e-")
    workdir = str(Path(workdir).resolve())
    Path(workdir).mkdir(parents=True, exist_ok=True)
    log("workdir", workdir)

    answer_path = Path(workdir) / "answer.txt"

    os.environ.setdefault("HERMES_DELEGATE_SESSION_BACKEND", "pi")  # default untouched

    import tools.delegate_session_tool as ds

    if not ds.check_delegate_session_requirements():
        log("FAIL", "neither pi nor opencode available")
        return 1

    parent = ParentStub(secret)
    goal = (
        "You must use the built-in ask tool to ask the supervisor one question: "
        "'What is the project password?' The question must offer these exact "
        "options: 'sunset-ferret-42' and 'blue-otter-17'. After receiving the "
        f"answer via the ask tool, write the received answer to the exact file "
        f"path {answer_path} (contents: exactly the answer you received, single "
        "line), then reply with DONE and the answer. Do the file write yourself "
        "with the write tool; do not delegate it to a subagent."
    )

    log("starting opencode delegate session")
    raw = ds.delegate_session(
        action="start", goal=goal, backend="opencode",
        timeout=600, parent_agent=parent,
    )
    start_payload = json.loads(raw)
    if not start_payload.get("success"):
        log("FAIL", f"start failed: {raw[:1000]}")
        return 1
    sid = start_payload["session_id"]
    log("session started", f"id={sid} backend={start_payload.get('backend')} native={start_payload.get('native_session_id')}")

    # Watch the turn; record any question that was answered.
    answered: list[dict] = []
    original = ds._auto_answer_delegate_question

    def spy(parent_agent, backend_name, method, question, options):
        answer = original(parent_agent, backend_name, method, question, options)
        answered.append({"method": method, "question": question, "answer": answer})
        log("question auto-answered", f"{method}: {question!r} -> {answer!r}")
        return answer

    ds._auto_answer_delegate_question = spy
    try:
        deadline = time.time() + 600
        final_text = None
        while time.time() < deadline:
            status = json.loads(ds.delegate_session(action="status", session_id=sid, parent_agent=parent))
            if status.get("error"):
                log("FAIL", f"session error: {status['error']}")
                return 1
            last = (status.get("last_result") or {}).get("text")
            if status.get("status") == "idle" and last:
                final_text = last
                break
            time.sleep(3)
    finally:
        ds._auto_answer_delegate_question = original

    if final_text is None:
        log("FAIL", "turn never produced a result")
        return 1
    log("turn finished", f"result: {final_text[:300]}")

    ok = True
    if not answered:
        log("FAIL", "no question.asked was observed/replied")
        ok = False
    else:
        q = answered[0]
        if secret not in str(q.get("answer")):
            log("FAIL", f"auto-answer did not contain the secret: {q.get('answer')!r}")
            ok = False

    answer_file = Path(workdir) / "answer.txt"
    if not answer_file.is_file():
        log("FAIL", "delegate did not write answer.txt")
        ok = False
    else:
        contents = answer_file.read_text().strip()
        log("answer.txt", contents)
        if secret not in contents:
            log("FAIL", "answer.txt does not contain the Hermes-provided answer")
            ok = False

    # Transcript check: the reply payload we sent shows up in the session messages.
    raw_msgs = ds.delegate_session(action="messages", session_id=sid, parent_agent=parent)
    msgs_payload = json.loads(raw_msgs)
    transcript = msgs_payload.get("messages_json", "")
    if secret not in transcript:
        log("FAIL", "session transcript does not contain the auto-answered secret")
        ok = False
    else:
        log("transcript contains Hermes-generated answer")

    try:
        ds.delegate_session(action="stop", session_id=sid, parent_agent=parent)
        log("session stopped")
    finally:
        if workdir.startswith(tempfile.gettempdir()):
            shutil.rmtree(workdir, ignore_errors=True)

    log("RESULT", "PASS — OpenCode question answered autonomously by Hermes" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
