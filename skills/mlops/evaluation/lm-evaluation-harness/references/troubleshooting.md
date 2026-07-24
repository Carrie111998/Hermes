# Troubleshooting

Common lm-evaluation-harness failures and their fixes. For multi-GPU-specific
hangs and NCCL errors see [distributed-eval.md](distributed-eval.md); for API
auth/rate-limit/cost issues see [api-evaluation.md](api-evaluation.md).

## Evaluation too slow

Use the vLLM backend:
```bash
lm_eval --model vllm \
  --model_args pretrained=<model>,tensor_parallel_size=2
```

Or reduce few-shot examples:
```bash
--num_fewshot 0  # Instead of 5
```

Or evaluate a subset of MMLU:
```bash
--tasks mmlu_stem  # Only STEM subjects
```

## Out of memory

Reduce batch size:
```bash
--batch_size 1  # Or --batch_size auto
```

Use quantization:
```bash
--model_args pretrained=<model>,load_in_8bit=True
```

Enable CPU offloading:
```bash
--model_args pretrained=<model>,device_map=auto,offload_folder=offload
```

## Different results than reported

Check few-shot count:
```bash
--num_fewshot 5  # Most papers use 5-shot
```

Check the exact task name:
```bash
--tasks mmlu  # Not mmlu_direct or mmlu_fewshot
```

Verify model and tokenizer match:
```bash
--model_args pretrained=<model>,tokenizer=<model>
```

Also confirm dtype (`bfloat16` vs `float16` vs quantized) — quantization alone
can move MMLU by several points.

## HumanEval not executing code

Install execution dependencies:
```bash
pip install human-eval
```

Enable code execution:
```bash
lm_eval --model hf \
  --model_args pretrained=<model> \
  --tasks humaneval \
  --allow_code_execution  # Required for HumanEval
```

Only run generated code in a sandbox or throwaway container — `--allow_code_execution`
executes untrusted model output on the eval host.
