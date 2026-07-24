---
name: modal-serverless-gpu
description: Serverless GPU cloud platform for running ML workloads. Use when you need on-demand GPU access without infrastructure management, deploying ML models as APIs, or running batch jobs with automatic scaling.
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [modal>=0.64.0]
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Infrastructure, Serverless, GPU, Cloud, Deployment, Modal]

---

# Modal Serverless GPU

Run ML workloads on Modal's serverless GPU cloud: infrastructure defined in Python, scale to zero, pay per second.

## When to use Modal

**Use Modal when:**
- Running GPU-intensive ML workloads without managing infrastructure
- Deploying ML models as auto-scaling APIs
- Running batch processing jobs (training, inference, data processing)
- Need pay-per-second GPU pricing without idle costs
- Prototyping ML applications quickly
- Running scheduled jobs (cron-like workloads)

**Key features:**
- **Serverless GPUs**: T4, L4, A10G, L40S, A100, H100, H200, B200 on-demand
- **Python-native**: Define infrastructure in Python code, no YAML
- **Auto-scaling**: Scale to zero, scale to 100+ GPUs instantly
- **Sub-second cold starts**: Rust-based infrastructure for fast container launches
- **Container caching**: Image layers cached for rapid iteration
- **Web endpoints**: Deploy functions as REST APIs with zero-downtime updates

**Use alternatives instead:**
- **RunPod**: For longer-running pods with persistent state
- **Lambda Labs**: For reserved GPU instances
- **SkyPilot**: For multi-cloud orchestration and cost optimization
- **Kubernetes**: For complex multi-service architectures

## Minimal end-to-end skeleton

```bash
pip install modal
modal setup  # Opens browser for authentication
```

```python
import modal

app = modal.App("text-generation")
image = modal.Image.debian_slim().pip_install("transformers", "torch", "accelerate")

@app.cls(gpu="A10G", image=image)
class TextGenerator:
    @modal.enter()                      # runs once per container, during warm-up
    def load_model(self):
        from transformers import pipeline
        self.pipe = pipeline("text-generation", model="<model>", device=0)

    @modal.method()
    def generate(self, prompt: str) -> str:
        return self.pipe(prompt, max_length=100)[0]["generated_text"]

@app.local_entrypoint()
def main():
    print(TextGenerator().generate.remote("Hello, world"))
```

`modal run app.py` to execute once, `modal serve app.py` for live-reload development, `modal deploy app.py` to leave it running.

## Where to read more

| To do this | Read |
|------------|------|
| Pick a GPU, spec `gpu=` / multi-GPU / fallbacks, size memory-cpu-timeout | [references/api-reference.md](references/api-reference.md) |
| Build container images (pip, apt, CUDA base registry) | [references/api-reference.md](references/api-reference.md) |
| Persist models/data in a Volume, mount secrets, schedule cron jobs | [references/api-reference.md](references/api-reference.md) |
| Expose a web endpoint (fastapi_endpoint / asgi_app / wsgi_app / web_server) | [references/api-reference.md](references/api-reference.md) |
| Dynamic batching, `.map()` fan-out, cold-start tuning | [references/api-reference.md](references/api-reference.md) |
| Multi-GPU & DeepSpeed training, WebSockets, streaming, auth, cost optimization, monitoring, production envs, Sandboxes | [references/advanced-usage.md](references/advanced-usage.md) |
| Diagnose auth failures, image build failures, GPU OOM, stale volumes, 502s, missing secrets, cron not firing | [references/troubleshooting.md](references/troubleshooting.md) |

## Key constraints

- **`@modal.enter()` for model loading.** Loading inside the request method pays the cost on every call.
- **`volume.commit()` is required** after writing to a Volume, or other containers will not see the data.
- **Schedules run in UTC.** `modal.Cron("0 8 * * *")` is 08:00 UTC, not local time.
- **Scheduled functions must be `modal deploy`ed**, not `modal run`.
- **Max 8 GPUs per function.** Beyond that you need multi-container coordination.
- **Pin dependency versions in images** — unpinned builds break unpredictably and defeat layer caching.

## Common issues (quick table)

| Issue | Solution |
|-------|----------|
| Cold start latency | Increase `container_idle_timeout`, use `@modal.enter()` |
| GPU OOM | Use larger GPU (`A100-80GB`), enable gradient checkpointing |
| Image build fails | Pin dependency versions, check CUDA compatibility |
| Timeout errors | Increase `timeout`, add checkpointing |

Full diagnosis trees for each: [references/troubleshooting.md](references/troubleshooting.md).
