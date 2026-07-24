---
name: evaluating-llms-harness
description: "lm-eval-harness: benchmark LLMs (MMLU, GSM8K, etc.)."
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [lm-eval, transformers, vllm]
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Evaluation, LM Evaluation Harness, Benchmarking, MMLU, HumanEval, GSM8K, EleutherAI, Model Quality, Academic Benchmarks, Industry Standard]

---

# lm-evaluation-harness - LLM Benchmarking

## What's inside

Evaluates LLMs across 60+ academic benchmarks (MMLU, HumanEval, GSM8K, TruthfulQA, HellaSwag). Use when benchmarking model quality, comparing models, reporting academic results, or tracking training progress. Industry standard used by EleutherAI, HuggingFace, and major labs. Supports HuggingFace, vLLM, APIs.

## When to use this skill

**Use lm-evaluation-harness when:**
- Benchmarking models for academic papers
- Comparing model quality across standard tasks
- Tracking training progress across checkpoints
- Reporting standardized metrics (everyone uses the same prompts)
- Need reproducible evaluation

**Use alternatives instead:**
- **HELM** (Stanford): broader evaluation (fairness, efficiency, calibration)
- **AlpacaEval**: instruction-following evaluation with LLM judges
- **MT-Bench**: conversational multi-turn evaluation
- **Custom scripts**: domain-specific evaluation

## Red lines (non-negotiable)

1. **Never hardcode API keys.** `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` come from the
   environment or a secret manager, never from a shell script or config committed to git.
2. **Never launch a paid API evaluation without a cost estimate first.** Run with
   `--limit 20` and multiply out to full size before the real run. A full MMLU run against
   a frontier API is thousands of requests; a runaway loop bills real money.
3. **Treat every full-suite run as a long-running job.** Full MMLU on a 7B model is ~2 hours
   on one A100; a 70B multi-GPU run is longer. Run under `tmux`/`nohup`/SLURM with
   `--output_path` set, never in a foreground shell that a disconnect can kill.
4. **Never report a number without its `--num_fewshot`, dtype, and `*_stderr`.** Scores
   without the eval config are unreproducible and not comparable to published results.
5. **`--allow_code_execution` runs model-generated code on the host.** Sandbox or
   throwaway container only.
6. **Never weaken a benchmark to make a score look better** (task subsetting, custom
   filters, cherry-picked few-shot counts) unless the change is reported alongside the score.

## End-to-end skeleton

```bash
pip install lm-eval

# 1. See what tasks exist
lm_eval --tasks list

# 2. Smoke-test the config cheaply (20 samples/task)
lm_eval --model hf \
  --model_args pretrained=<model>,dtype=bfloat16 \
  --tasks mmlu,gsm8k \
  --device cuda:0 --batch_size auto \
  --limit 20 --output_path results/smoke.json

# 3. Real run, standard suite, 5-shot, detached
nohup lm_eval --model hf \
  --model_args pretrained=<model>,dtype=bfloat16 \
  --tasks mmlu,gsm8k,hellaswag,truthfulqa,arc_challenge \
  --num_fewshot 5 \
  --device cuda:0 --batch_size auto \
  --output_path results/model-eval.json \
  --log_samples > eval.log 2>&1 &

# 4. Read the primary metric + stderr per task out of results/model-eval.json
```

## Where to go next

| To do this | Read this |
|---|---|
| Pick a backend, set `--model_args`, look up a CLI flag, size GPU/VRAM/time | `references/cli-reference.md` |
| Understand what a benchmark measures, its metric, and score expectations | `references/benchmark-guide.md` |
| Parse the result JSON, build a model comparison table, plot training curves | `references/result-analysis.md` |
| Evaluate an OpenAI / Anthropic / local OpenAI-compatible endpoint, manage API cost | `references/api-evaluation.md` |
| Write a domain-specific task (YAML config, prompt templates, custom metrics) | `references/custom-tasks.md` |
| Run multi-GPU or multi-node evaluation (data / tensor / pipeline parallel, SLURM) | `references/distributed-eval.md` |
| Fix slow runs, OOM, mismatched scores, HumanEval not executing | `references/troubleshooting.md` |

## Resources

- GitHub: https://github.com/EleutherAI/lm-evaluation-harness
- Docs: https://github.com/EleutherAI/lm-evaluation-harness/tree/main/docs
- Task library: 60+ tasks including MMLU, GSM8K, HumanEval, TruthfulQA, HellaSwag, ARC, WinoGrande, etc.
- Leaderboard: https://huggingface.co/spaces/HuggingFaceH4/open_llm_leaderboard (uses this harness)
