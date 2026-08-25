"""Explanation-policy eval runner (#93382, Markdown-only slice).

Pipeline per policy, on one concept-comparison task:
  1. Apply the policy to the task's signals -> a modality.
  2. Explain the concept under that modality's render instruction. Markdown
     out, always -- no artifact envelope.
  3. Hand that explanation, and nothing else, to a fresh reader model, which
     answers the comprehension and transfer items with a confidence each.
  4. Judge those answers with a separate call that sees gold; the reader
     never does.
  5. Emit scorecard.json for report.py.

The reader is a PROXY, not a human subject: this measures what survives an
explanation, which is a prerequisite for the human study #93382 describes,
not a substitute for it.

Run from the repo root, venv active, provider configured:

    python evals/explanation_policy/runner.py --out evals/explanation_policy/results/run1
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from evals.explanation_policy.fixtures import Task, get_task  # noqa: E402
from evals.explanation_policy.policies import (  # noqa: E402
    EVAL_MODEL,
    POLICIES,
    RENDER_INSTRUCTIONS,
    Intent,
    Signals,
    Structure,
    select,
)

EXPLAIN_PROMPT = """You are answering a user's question. Follow the format instruction exactly.

Question: {question}

Format instruction: {instruction}

Cover these points; do not add unrelated material:
{gold}
"""

READ_PROMPT = """Read the explanation below, then answer the questions using ONLY what it taught you.

--- explanation ---
{explanation}
--- end explanation ---

Answer each question, and rate your confidence that your answer is correct
from 0 to 100. Reply with JSON only:

{{"answers": [{{"q": "<question>", "a": "<your answer>", "confidence": <0-100>}}]}}

Questions:
{questions}
"""

JUDGE_PROMPT = """Score a reader's answers against the reference. Be strict: an answer
scores 1 only if it is substantively correct, 0 otherwise. Restating the
question, or a vague gesture at the right area, scores 0.

Reference:
{gold}

Reader's answers:
{answers}

Reply with JSON only: {{"scores": [0 or 1, ...]}} in the same order.
"""


def _call(prompt: str, task: str, model: str | None, timeout: float,
          max_tokens: int = 1500):
    """Return (text, model_the_provider_reported).

    With no `model` the configured auxiliary route decides, which is how the
    other harnesses in `evals/` call this. We record what came back rather
    than what we asked for: a scorecard has to name the model that actually
    produced it, and a route can answer with something else entirely.

    `timeout` is passed explicitly on purpose. These task names are not in
    anyone's `auxiliary.*` config, so they fall back to _DEFAULT_AUX_TIMEOUT
    (30s) -- far too short for a model composing a worked example, and the
    failure looks like a provider problem rather than a config default.
    """
    from agent.auxiliary_client import call_llm

    kwargs = {"messages": [{"role": "user", "content": prompt}],
              "task": task, "max_tokens": max_tokens, "timeout": timeout}
    if model:
        kwargs["model"] = model
    resp = call_llm(**kwargs)
    reported = getattr(resp, "model", None) or model or "unknown"
    if hasattr(resp, "choices"):
        return resp.choices[0].message.content or "", reported
    return str(resp), reported


def _extract_json(text: str):
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    start = min([i for i in (text.find("["), text.find("{")) if i >= 0], default=0)
    return json.loads(text[start:])


def run_policy(policy: str, task: Task, signals: Signals, model: str | None,
               timeout: float) -> dict:
    started = time.time()
    modality = select(policy, signals)

    explanation, m1 = _call(
        EXPLAIN_PROMPT.format(
            question=task.question,
            instruction=RENDER_INSTRUCTIONS[modality],
            gold=task.gold,
        ),
        task="explanation_policy_explain",
        model=model,
        timeout=timeout,
    )

    questions = list(task.comprehension) + [task.transfer]
    raw, m2 = _call(
        READ_PROMPT.format(
            explanation=explanation,
            questions="\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions)),
        ),
        task="explanation_policy_read",
        model=model,
        timeout=timeout,
    )
    answers = _extract_json(raw)["answers"]

    gold = task.gold + f"\n\nTransfer item -- correct answer:\n{task.transfer_gold}"
    judged, m3 = _call(
        JUDGE_PROMPT.format(
            gold=gold,
            answers=json.dumps(
                [{"q": a.get("q"), "a": a.get("a")} for a in answers], indent=2
            ),
        ),
        task="explanation_policy_judge",
        model=model,
        timeout=timeout,
        max_tokens=400,
    )
    scored = _extract_json(judged)["scores"]

    n_comp = len(task.comprehension)
    comp = scored[:n_comp]
    transfer = scored[n_comp:]
    confidences = [float(a.get("confidence", 0)) / 100 for a in answers]
    accuracy = sum(scored) / len(scored) if scored else 0.0
    mean_conf = sum(confidences) / len(confidences) if confidences else 0.0

    return {
        "policy": policy,
        "label": POLICIES[policy]["label"],
        "modality": modality.value,
        "models": sorted({m1, m2, m3}),
        "comprehension_pct": round(100 * sum(comp) / len(comp)) if comp else 0,
        "transfer_pct": round(100 * sum(transfer) / len(transfer)) if transfer else 0,
        "calibration_error": round(abs(mean_conf - accuracy), 3),
        "explanation_chars": len(explanation),
        "seconds": round(time.time() - started, 1),
        "explanation": explanation,
        "answers": answers,
        "scores": scored,
    }


def summarize(policy: str, rs: list) -> dict:
    """Average one policy's repeats into a scorecard row."""
    def avg(key: str, digits: int = 0):
        value = sum(r[key] for r in rs) / len(rs)
        return round(value, digits) if digits else round(value)

    return {
        "policy": policy,
        "label": rs[0]["label"],
        "modality": rs[0]["modality"],
        "n": len(rs),
        "comprehension_pct": avg("comprehension_pct"),
        "transfer_pct": avg("transfer_pct"),
        "calibration_error": avg("calibration_error", 3),
        "explanation_chars": avg("explanation_chars"),
        "seconds": avg("seconds", 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="value_vs_reference")
    ap.add_argument("--policies", default=",".join(POLICIES))
    ap.add_argument("--intent", default="learn", choices=[i.value for i in Intent])
    ap.add_argument("--structure", default="comparison", choices=[s.value for s in Structure])
    ap.add_argument("--knowledge", default="unknown", choices=["unknown", "novice", "practitioner"])
    ap.add_argument("--repeats", type=int, default=1, help="runs per policy; report averages")
    ap.add_argument(
        "--model",
        default=None,
        help=(f"force a model for every arm. Default: let the configured auxiliary "
              f"route decide, as the other evals/ harnesses do. Published reference "
              f"scorecards use {EVAL_MODEL}; whatever runs, the model the provider "
              f"reports is recorded on every scorecard row."),
    )
    ap.add_argument(
        "--timeout", type=float, default=180.0,
        help=("per-call timeout in seconds. These task names are in nobody's "
              "auxiliary config, so without this the 30s aux default applies "
              "and a slow explanation looks like a provider failure."),
    )
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    task = get_task(args.task)
    signals = Signals(
        intent=Intent(args.intent),
        structure=Structure(args.structure),
        knowledge=args.knowledge,
    )
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    runs = []
    for policy in args.policies.split(","):
        policy = policy.strip()
        if policy not in POLICIES:
            print(f"skipping unknown policy: {policy}")
            continue
        for i in range(args.repeats):
            print(f"[{policy}] run {i + 1}/{args.repeats} ...", flush=True)
            runs.append(run_policy(policy, task, signals, args.model, args.timeout))

    (out_dir / "runs.json").write_text(json.dumps(runs, indent=2), encoding="utf-8")

    # The model goes on every row: a scorecard that does not say which model
    # produced it cannot be compared to any other scorecard.
    models = sorted({m for r in runs for m in r["models"]})
    card = [dict(summarize(policy, [r for r in runs if r["policy"] == policy]),
                 model=", ".join(models), task=task.key)
            for policy in dict.fromkeys(r["policy"] for r in runs)]
    (out_dir / "scorecard.json").write_text(json.dumps(card, indent=2), encoding="utf-8")
    print(f"\nwrote {out_dir}/scorecard.json  ({len(runs)} runs)")
    print(f"render it with: python evals/explanation_policy/report.py {out_dir}")


if __name__ == "__main__":
    main()
