"""Opt-in output guards for oneshot (-z) mode.

Two independent, config-gated mechanisms (both default OFF — vanilla behavior
is unchanged unless the user enables them in config.yaml):

1. Output-contract enforcement (``oneshot.forbid_code_final``)
   A final answer that still contains a fenced code block is treated as
   unfinished work: the agent is asked (bounded retries) to finish with the
   answer itself. Motivation: in headless pipelines the final stdout IS the
   deliverable; a trailing code block usually means the model ran out of its
   own steam mid-task and the caller silently receives code instead of an
   answer.

2. Independent verifier gate (``oneshot.verifier``)
   After the final answer is produced, a separate (ideally different-family)
   model checks it against the original prompt. On FAIL, the agent gets the
   verifier's issues as feedback and one or more retry turns. The verifier
   never writes the answer — it only gates it. Fails open on any
   infrastructure error: a broken verifier must not take down the run.

Config::

    oneshot:
      forbid_code_final: true      # default false
      finalize_retries: 2          # follow-up turns to obtain a code-free answer
      verifier:
        enabled: true              # default false
        model: "google/gemma-3-12b-it"
        base_url: "https://openrouter.ai/api/v1"   # OpenAI-compatible
        api_key_env: "OPENROUTER_API_KEY"
        max_retries: 1             # agent retry turns after FAIL
        timeout_s: 120

Every guard decision is recorded under ``result["hw_guard"]`` so callers
(and ``--usage-file`` style pipelines) can observe what happened.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

_CODE_BLOCK_RE = re.compile(r"```[^\n`]*\n.*?```", re.S)

FINALIZE_MSG = (
    "Your previous reply contained a code block, but this is a non-interactive "
    "run whose final reply must be the answer itself. Using the results you "
    "already produced, reply now with ONLY the final answer in the requested "
    "format — no code blocks."
)

FEEDBACK_TMPL = (
    "An independent reviewer rejected your answer for these reasons:\n- {issues}\n"
    "Address every issue and reply with a corrected final answer in the "
    "requested format."
)

VERIFY_PROMPT_TMPL = (
    "You are a strict verifier. An autonomous agent was given the task below "
    "and produced the final answer below. Judge ONLY whether the answer "
    "satisfies the task as stated: required output format present, all "
    "requested parts answered, instructions and stated definitions respected. "
    "Do not produce a replacement answer.\n"
    'Reply with JSON only: {{"status": "PASS" | "FAIL", "issues": ["..."]}}\n\n'
    "TASK:\n{task}\n\nFINAL ANSWER:\n{answer}"
)


@dataclass
class GuardConfig:
    forbid_code_final: bool = False
    finalize_retries: int = 2
    verifier_enabled: bool = False
    verifier_model: str = ""
    verifier_base_url: str = "https://openrouter.ai/api/v1"
    verifier_api_key_env: str = "OPENROUTER_API_KEY"
    verifier_max_retries: int = 1
    verifier_timeout_s: int = 120
    extra: dict = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.forbid_code_final or (self.verifier_enabled and bool(self.verifier_model))


def load_guard_config(cfg: dict) -> GuardConfig:
    oneshot = cfg.get("oneshot") if isinstance(cfg.get("oneshot"), dict) else {}
    verifier = oneshot.get("verifier") if isinstance(oneshot.get("verifier"), dict) else {}
    return GuardConfig(
        forbid_code_final=bool(oneshot.get("forbid_code_final", False)),
        finalize_retries=max(0, int(oneshot.get("finalize_retries", 2) or 0)),
        verifier_enabled=bool(verifier.get("enabled", False)),
        verifier_model=str(verifier.get("model") or ""),
        verifier_base_url=str(verifier.get("base_url") or "https://openrouter.ai/api/v1"),
        verifier_api_key_env=str(verifier.get("api_key_env") or "OPENROUTER_API_KEY"),
        verifier_max_retries=max(0, int(verifier.get("max_retries", 1) or 0)),
        verifier_timeout_s=max(1, int(verifier.get("timeout_s", 120) or 1)),
    )


def has_code_final(text: str) -> bool:
    return bool(_CODE_BLOCK_RE.search(text or ""))


def _parse_verdict(text: str) -> Optional[dict]:
    try:
        out = json.loads(text)
        if isinstance(out, dict) and "status" in out:
            return out
    except (json.JSONDecodeError, TypeError):
        pass
    for cand in reversed(re.findall(r"\{.*?\}", text or "", re.S)):
        try:
            out = json.loads(cand)
            if isinstance(out, dict) and "status" in out:
                return out
        except json.JSONDecodeError:
            continue
    return None


def verify_answer(task_prompt: str, answer: str, gcfg: GuardConfig) -> dict:
    """One verifier call. Fails open (PASS + error note) on any failure."""
    api_key = os.environ.get(gcfg.verifier_api_key_env, "")
    if not api_key:
        return {"status": "PASS", "issues": [], "verifier_error": "no api key"}
    try:
        import requests

        resp = requests.post(
            gcfg.verifier_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": gcfg.verifier_model,
                "messages": [{
                    "role": "user",
                    "content": VERIFY_PROMPT_TMPL.format(
                        task=(task_prompt or "")[:6000], answer=(answer or "")[:6000]
                    ),
                }],
                "temperature": 0.0,
                "max_tokens": 500,
            },
            timeout=gcfg.verifier_timeout_s,
        )
        if resp.status_code != 200:
            return {"status": "PASS", "issues": [], "verifier_error": f"http {resp.status_code}"}
        verdict = _parse_verdict(resp.json()["choices"][0]["message"]["content"] or "")
        if verdict is None:
            return {"status": "PASS", "issues": [], "verifier_error": "unparseable verdict"}
        status = str(verdict.get("status", "PASS")).upper()
        issues = [str(i) for i in verdict.get("issues", []) if str(i).strip()]
        return {"status": "FAIL" if status == "FAIL" else "PASS", "issues": issues}
    except Exception as exc:  # noqa: BLE001 — guard must never sink the run
        return {"status": "PASS", "issues": [], "verifier_error": repr(exc)}


def _continue(agent: Any, message: str) -> Optional[dict]:
    """One follow-up turn continuing the existing conversation. Returns the
    run result, or None when continuation isn't possible."""
    history = getattr(agent, "_session_messages", None)
    if not isinstance(history, list) or not history:
        return None
    return agent.run_conversation(message, conversation_history=history)


def _enforce_contract(agent: Any, response: str, result: dict, gcfg: GuardConfig,
                      log: dict) -> tuple[str, dict]:
    tries = 0
    while gcfg.forbid_code_final and has_code_final(response) and tries < gcfg.finalize_retries:
        tries += 1
        log["finalize_turns"] = tries
        nxt = _continue(agent, FINALIZE_MSG)
        if nxt is None:
            log["finalize_aborted"] = "no session history to continue"
            break
        result = nxt
        response = result.get("final_response") or response
    log["contract_violated_at_end"] = gcfg.forbid_code_final and has_code_final(response)
    return response, result


def run_guarded(agent: Any, prompt: str, gcfg: GuardConfig) -> tuple[str, dict]:
    """Drop-in replacement for the plain ``run_conversation`` call in oneshot
    mode. Returns ``(final_response, run_result)`` with guard telemetry in
    ``run_result["hw_guard"]``."""
    log: dict = {}
    result = agent.run_conversation(prompt)
    response = result.get("final_response") or ""

    response, result = _enforce_contract(agent, response, result, gcfg, log)

    if gcfg.verifier_enabled and gcfg.verifier_model:
        verifications = log.setdefault("verifications", [])
        for attempt in range(gcfg.verifier_max_retries + 1):
            verdict = verify_answer(prompt, response, gcfg)
            verifications.append(verdict)
            if verdict["status"] != "FAIL" or attempt >= gcfg.verifier_max_retries:
                break
            issues = "\n- ".join(verdict["issues"][:6]) or "answer failed verification"
            nxt = _continue(agent, FEEDBACK_TMPL.format(issues=issues))
            if nxt is None:
                log["verify_retry_aborted"] = "no session history to continue"
                break
            result = nxt
            response = result.get("final_response") or response
            response, result = _enforce_contract(agent, response, result, gcfg, log)

    if isinstance(result, dict):
        result["hw_guard"] = log
    return response, result
