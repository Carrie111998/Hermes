---
name: optimizing-attention-flash
description: Optimizes transformer attention with Flash Attention for 2-4x speedup and 10-20x memory reduction. Use when training/running transformers with long sequences (>512 tokens), encountering GPU memory issues with attention, or need faster inference. Supports PyTorch native SDPA, flash-attn library, H100 FP8, and sliding window attention.
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [flash-attn, torch, transformers]
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Optimization, Flash Attention, Attention Optimization, Memory Efficiency, Speed Optimization, Long Context, PyTorch, SDPA, H100, FP8, Transformers]

---

# Flash Attention - Fast Memory-Efficient Attention

Flash Attention gives 2-4x speedup and 10-20x attention-memory reduction through IO-aware tiling and recomputation, with exact (not approximate) results.

## When to use this skill

**Use Flash Attention when:**
- Training transformers with sequences >512 tokens
- Running inference with long context (>2K tokens)
- GPU memory constrained (OOM with standard attention)
- You need 2-4x speedup without accuracy loss
- Using PyTorch 2.2+ or able to install `flash-attn`

**Do NOT use it when:**
- Sequences <256 tokens → standard attention (kernel overhead is not worth it)
- You need attention *variants* rather than raw speed → **xFormers**
- CPU inference → Flash Attention requires a GPU (Ampere+/Turing; **not** V100)
- Activations are float32 → unsupported; must be fp16/bf16

## Routing table

| To do this | Read |
|------------|------|
| Convert an existing PyTorch model to native SDPA, force the Flash backend, profile it, verify parity | [references/sdpa-native.md](references/sdpa-native.md) |
| Use the `flash_attn` package: install, layout, MQA/GQA, sliding window, microbenchmarks, H100 FP8 / FlashAttention-3 | [references/flash-attn-library.md](references/flash-attn-library.md) |
| Enable Flash Attention in HuggingFace models (Llama, Mistral, BERT, GPT), fine-tuning and multi-GPU configs, model-specific issues | [references/transformers-integration.md](references/transformers-integration.md) |
| Check GPU/CUDA/dtype support, or fix ImportError / CUDA error / no-speedup / accuracy issues | [references/hardware-compatibility.md](references/hardware-compatibility.md) |
| Cite speed, memory and scaling numbers per GPU, sequence length, and FA version | [references/benchmarks.md](references/benchmarks.md) |

## Key constraints and gotchas

- **Tensor layout differs between the two APIs.** SDPA wants `[batch, heads, seq, dim]`; `flash_attn_func` wants `[batch, seq, heads, dim]`. Transposing in the wrong direction silently produces garbage.
- **dtype must be fp16 or bf16.** float32 falls back or errors — never assume the Flash kernel ran.
- **SDPA silently picks a backend.** Wrap in `torch.backends.cuda.sdp_kernel(enable_flash=True, enable_math=False, enable_mem_efficient=False)` when you must prove Flash was used.
- **Speedup scales with sequence length**: ~10-20% under 512 tokens, 2-3x at 512-2K, 3-4x above 2K.
- **Install needs `--no-build-isolation`**, and a matching CUDA toolkit (12.0+, 11.8 minimum).
- **Memory is not increased**, but it is also not reduced below the model's other activations — Flash only removes the O(seq²) attention matrix.
- **Expected numerical difference vs standard attention is <1e-3 in fp16** — larger deltas mean a real bug, not kernel noise.

## End-to-end skeleton

```python
import torch
import torch.nn.functional as F

q = torch.randn(2, 8, 2048, 64, device='cuda', dtype=torch.float16)  # [b, h, s, d]
k = torch.randn(2, 8, 2048, 64, device='cuda', dtype=torch.float16)
v = torch.randn(2, 8, 2048, 64, device='cuda', dtype=torch.float16)

# Path A - PyTorch native (easiest, PyTorch 2.2+)
with torch.backends.cuda.sdp_kernel(enable_flash=True, enable_math=False,
                                    enable_mem_efficient=False):
    out = F.scaled_dot_product_attention(q, k, v, is_causal=True)

# Path B - flash-attn library (MQA, sliding window, FP8)
# pip install flash-attn --no-build-isolation
from flash_attn import flash_attn_func
out = flash_attn_func(
    q.transpose(1, 2), k.transpose(1, 2), v.transpose(1, 2),  # [b, s, h, d]
    dropout_p=0.0, causal=True,
).transpose(1, 2)

# Path C - HuggingFace model
from transformers import AutoModelForCausalLM
model = AutoModelForCausalLM.from_pretrained(
    "meta-llama/Llama-2-7b-hf",
    torch_dtype=torch.float16,
    attn_implementation="flash_attention_2",
    device_map="auto",
)
```
