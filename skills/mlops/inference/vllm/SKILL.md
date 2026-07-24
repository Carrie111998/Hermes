---
name: serving-llms-vllm
description: "vLLM: high-throughput LLM serving, OpenAI API, quantization."
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [vllm, torch, transformers]
platforms: [linux, macos]
metadata:
  hermes:
    tags: [vLLM, Inference Serving, PagedAttention, Continuous Batching, High Throughput, Production, OpenAI API, Quantization, Tensor Parallelism]

---

# vLLM - High-Performance LLM Serving

## When to use this skill

Use when deploying production LLM APIs, optimizing inference latency/throughput, or serving models with limited GPU memory. Supports OpenAI-compatible endpoints, quantization (GPTQ/AWQ/FP8), and tensor parallelism.

vLLM achieves 24x higher throughput than standard transformers through PagedAttention (block-based KV cache) and continuous batching (mixing prefill/decode requests).

**Use vLLM when:**
- Deploying production LLM APIs (100+ req/sec)
- Serving OpenAI-compatible endpoints
- Limited GPU memory but need large models
- Multi-user applications (chatbots, assistants)
- Need low latency with high throughput

**Use alternatives instead:**
- **llama.cpp**: CPU/edge inference, single-user
- **HuggingFace transformers**: Research, prototyping, one-off generation
- **TensorRT-LLM**: NVIDIA-only, need absolute maximum performance
- **Text-Generation-Inference**: Already in HuggingFace ecosystem

## Red lines (non-negotiable)

- **VRAM decides the deployment, not the other way round:**
  - Small models (7B-13B): 1x A10 (24GB) or A100 (40GB)
  - Medium models (30B-40B): 2x A100 (40GB) with tensor parallelism
  - Large models (70B+): 4x A100 (40GB) or 2x A100 (80GB), use AWQ/GPTQ

  A 70B model does not fit on one 40GB GPU unquantized. Quantize or shard —
  there is no flag that makes it fit.
- **`--tensor-parallel-size` must be a power of 2** (1, 2, 4, 8) and divide the
  model's attention heads. `3` will run slower or fail outright.
- **`--gpu-memory-utilization` is preallocated, not a limit.** vLLM claims that
  fraction up front for the KV cache. Two vLLM processes at 0.9 on one GPU will
  OOM each other; give each GPU exactly one server.
- **`--quantization` must match how the checkpoint was quantized.** A mismatched
  value fails at load; do not guess.
- **First launch downloads full model weights** (tens to hundreds of GB for 70B
  class). Pre-stage weights and gate HuggingFace auth before a production rollout.
- **Model weights carry their own licence** (gated Llama repos, research-only
  checkpoints). Serving a model publicly is redistribution — check the model card
  terms, not just vLLM's Apache-2.0 licence.
- **Cost is dedicated GPU-hours, not per-token.** An idle vLLM server bills the
  same as a saturated one; size the fleet from measured throughput, and verify
  GPU utilization > 80% before adding replicas.
- Supported platforms: NVIDIA (primary), AMD ROCm, Intel GPUs, TPUs.

## Minimal end-to-end skeleton

```bash
pip install vllm
```

**Offline (in-process) inference:**

```python
from vllm import LLM, SamplingParams

llm = LLM(model="meta-llama/Llama-3-8B-Instruct")
sampling = SamplingParams(temperature=0.7, max_tokens=256)

outputs = llm.generate(["Explain quantum computing"], sampling)
print(outputs[0].outputs[0].text)
```

**OpenAI-compatible server:**

```bash
vllm serve meta-llama/Llama-3-8B-Instruct

# Query with OpenAI SDK
python -c "
from openai import OpenAI
client = OpenAI(base_url='http://localhost:8000/v1', api_key='EMPTY')
print(client.chat.completions.create(
    model='meta-llama/Llama-3-8B-Instruct',
    messages=[{'role': 'user', 'content': 'Hello!'}]
).choices[0].message.content)
"
```

Confirm readiness with `curl http://localhost:8000/health` before sending load.

## Routing table

| To do this | Read |
|------------|------|
| Deploy a server: Docker, Kubernetes, Nginx load balancing, multi-node, launch flags by model size, production rollout checklist, health checks and Prometheus | `references/server-deployment.md` |
| Run offline batch inference over a dataset, set sampling params, scale a batch job | `references/batch-inference.md` |
| Tune throughput and latency: PagedAttention internals, continuous batching, prefix caching, speculative decoding, benchmarks | `references/optimization.md` |
| Fit a large model in less VRAM: AWQ/GPTQ/FP8 setup, quantize your own checkpoint, accuracy trade-offs | `references/quantization.md` |
| Fix errors: OOM, low throughput, high TTFT, model-not-found, network, quantization and distributed failures, debug commands | `references/troubleshooting.md` |

## Resources

- Official docs: https://docs.vllm.ai
- GitHub: https://github.com/vllm-project/vllm
- Paper: "Efficient Memory Management for Large Language Model Serving with PagedAttention" (SOSP 2023)
- Community: https://discuss.vllm.ai
