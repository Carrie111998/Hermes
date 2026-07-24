# Offline Batch Inference

## Contents
- When to use offline batching
- Step-by-step batch workflow
- Sampling parameters
- Scaling notes

## When to use offline batching

Use the in-process `LLM` engine instead of `vllm serve` when processing a fixed
dataset: there is no HTTP overhead, no concurrency management, and vLLM's
continuous batching schedules the whole prompt list itself.

Use the server path instead when multiple clients or services need concurrent
access — see `server-deployment.md`.

## Step-by-step batch workflow

Copy this checklist:

```
Batch Processing:
- [ ] Step 1: Prepare input data
- [ ] Step 2: Configure LLM engine
- [ ] Step 3: Run batch inference
- [ ] Step 4: Process results
```

**Step 1: Prepare input data**

```python
# Load prompts from file
prompts = []
with open("prompts.txt") as f:
    prompts = [line.strip() for line in f]

print(f"Loaded {len(prompts)} prompts")
```

**Step 2: Configure LLM engine**

```python
from vllm import LLM, SamplingParams

llm = LLM(
    model="meta-llama/Llama-3-8B-Instruct",
    tensor_parallel_size=2,  # Use 2 GPUs
    gpu_memory_utilization=0.9,
    max_model_len=4096
)

sampling = SamplingParams(
    temperature=0.7,
    top_p=0.95,
    max_tokens=512,
    stop=["</s>", "\n\n"]
)
```

**Step 3: Run batch inference**

vLLM automatically batches requests for efficiency:

```python
# Process all prompts in one call
outputs = llm.generate(prompts, sampling)

# vLLM handles batching internally
# No need to manually chunk prompts
```

**Step 4: Process results**

```python
# Extract generated text
results = []
for output in outputs:
    prompt = output.prompt
    generated = output.outputs[0].text
    results.append({
        "prompt": prompt,
        "generated": generated,
        "tokens": len(output.outputs[0].token_ids)
    })

# Save to file
import json
with open("results.jsonl", "w") as f:
    for result in results:
        f.write(json.dumps(result) + "\n")

print(f"Processed {len(results)} prompts")
```

## Sampling parameters

| Parameter | Purpose |
|-----------|---------|
| `temperature` | Randomness; 0 = greedy/deterministic |
| `top_p` | Nucleus sampling cutoff |
| `max_tokens` | Hard cap on generated tokens per prompt |
| `stop` | Stop strings; generation ends before emitting them |
| `n` | Number of completions per prompt (multiplies compute) |

Output ordering matches the input `prompts` list, even though vLLM reorders
internally for scheduling.

## Scaling notes

- Do **not** chunk prompts manually in a Python loop; a single `generate()` call
  lets the scheduler keep the GPU saturated.
- `tensor_parallel_size` must be a power of 2 and divide the model's attention
  heads; see `troubleshooting.md` for the failure signature.
- `max_model_len` caps prompt + completion length and directly sizes the KV
  cache. Lower it when loading OOMs.
- Very large prompt lists are fine in one call, but hold results incrementally
  (write per batch) if memory for the Python-side result list becomes an issue.
- For throughput tuning (`max_num_seqs`, prefix caching, chunked prefill) see
  `optimization.md`.
