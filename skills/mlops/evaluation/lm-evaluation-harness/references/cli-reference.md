# CLI Reference: Backends, Flags, Hardware

How to install the harness, point it at a model, and size the hardware.

## Installation

```bash
pip install lm-eval

# Optional backends
pip install vllm        # 5-10x faster inference
pip install human-eval  # required for HumanEval code execution
```

## Discovering tasks

```bash
lm_eval --tasks list
```

## Benchmark suite selection

**Core reasoning benchmarks**:
- **MMLU** (Massive Multitask Language Understanding) - 57 subjects, multiple choice
- **GSM8K** - Grade school math word problems
- **HellaSwag** - Common sense reasoning
- **TruthfulQA** - Truthfulness and factuality
- **ARC** (AI2 Reasoning Challenge) - Science questions

**Code benchmarks**:
- **HumanEval** - Python code generation (164 problems)
- **MBPP** (Mostly Basic Python Problems) - Python coding

**Standard suite** (recommended for model releases):
```bash
--tasks mmlu,gsm8k,hellaswag,truthfulqa,arc_challenge
```

**Fast benchmarks** for frequent / in-training evaluation:
- **HellaSwag**: ~10 minutes on 1 GPU
- **GSM8K**: ~5 minutes
- **PIQA**: ~2 minutes

Avoid for frequent eval (too slow):
- **MMLU**: ~2 hours (57 subjects)
- **HumanEval**: requires code execution

## Model backends

### HuggingFace model

```bash
lm_eval --model hf \
  --model_args pretrained=<model>,dtype=bfloat16 \
  --tasks mmlu \
  --device cuda:0 \
  --batch_size auto  # Auto-detect optimal batch size
```

### Quantized model (4-bit / 8-bit)

```bash
lm_eval --model hf \
  --model_args pretrained=<model>,load_in_4bit=True \
  --tasks mmlu \
  --device cuda:0
```

### Custom checkpoint

```bash
lm_eval --model hf \
  --model_args pretrained=/path/to/my-model,tokenizer=/path/to/tokenizer \
  --tasks mmlu \
  --device cuda:0
```

### vLLM backend (5-10x faster)

```bash
lm_eval --model vllm \
  --model_args pretrained=<model>,tensor_parallel_size=1,dtype=auto,gpu_memory_utilization=0.8 \
  --tasks mmlu \
  --batch_size auto
```

Throughput comparison on the same 7B-class model, MMLU:

```bash
# Standard HF: ~2 hours
lm_eval --model hf \
  --model_args pretrained=<model> \
  --tasks mmlu \
  --batch_size 8

# vLLM: ~15-20 minutes
lm_eval --model vllm \
  --model_args pretrained=<model>,tensor_parallel_size=2 \
  --tasks mmlu \
  --batch_size auto
```

## Frequently used flags

| Flag | Purpose |
|------|---------|
| `--model` | Backend: `hf`, `vllm`, `openai-chat-completions`, `anthropic-chat`, `nemo_lm`, `sglang` |
| `--model_args` | Comma-separated backend args (`pretrained=`, `dtype=`, `load_in_8bit=`, `tensor_parallel_size=`, `device_map=`, `tokenizer=`) |
| `--tasks` | Comma-separated task names or task groups |
| `--num_fewshot` | Few-shot examples in the prompt; 5 is the paper default, 0 for speed |
| `--batch_size` | Fixed integer or `auto` |
| `--device` | e.g. `cuda:0` |
| `--output_path` | Directory or JSON file for results |
| `--log_samples` | Persist every individual prediction |
| `--limit N` | Only evaluate N samples per task (smoke tests, cost control) |
| `--allow_code_execution` | Required for HumanEval-style execution tasks |

## Full evaluation examples

```bash
# Full MMLU evaluation (57 subjects)
lm_eval --model hf \
  --model_args pretrained=<model> \
  --tasks mmlu \
  --num_fewshot 5 \
  --batch_size 8 \
  --output_path results/ \
  --log_samples

# Multiple benchmarks at once
lm_eval --model hf \
  --model_args pretrained=<model> \
  --tasks mmlu,gsm8k,hellaswag,truthfulqa,arc_challenge \
  --num_fewshot 5 \
  --batch_size 8 \
  --output_path results/model-eval.json
```

## Hardware requirements

- **GPU**: NVIDIA (CUDA 11.8+); CPU works but is very slow
- **VRAM**:
  - 7B model: 16GB (bf16) or 8GB (8-bit)
  - 13B model: 28GB (bf16) or 14GB (8-bit)
  - 70B model: requires multi-GPU or quantization
- **Time** (7B model, single A100):
  - HellaSwag: 10 minutes
  - GSM8K: 5 minutes
  - MMLU (full): 2 hours
  - HumanEval: 20 minutes

## Resources

- GitHub: https://github.com/EleutherAI/lm-evaluation-harness
- Docs: https://github.com/EleutherAI/lm-evaluation-harness/tree/main/docs
- Leaderboard: https://huggingface.co/spaces/HuggingFaceH4/open_llm_leaderboard (uses this harness)
