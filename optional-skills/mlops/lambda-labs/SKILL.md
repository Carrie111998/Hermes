---
name: lambda-labs-gpu-cloud
description: Reserved and on-demand GPU cloud instances for ML training and inference. Use when you need dedicated GPU instances with simple SSH access, persistent filesystems, or high-performance multi-node clusters for large-scale training.
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [lambda-cloud-client>=1.0.0]
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Infrastructure, GPU Cloud, Training, Inference, Lambda Labs]

---

# Lambda Labs GPU Cloud

Running ML workloads on Lambda Labs GPU cloud: on-demand instances and 1-Click
Slurm Clusters, driven from the console, the Python client, or plain `curl`.

## When to use Lambda Labs

**Use Lambda Labs when:**
- Need dedicated GPU instances with full SSH access
- Running long training jobs (hours to days)
- Want simple pricing with no egress fees
- Need persistent storage across sessions
- Require high-performance multi-node clusters (16-512 GPUs)
- Want pre-installed ML stack (Lambda Stack with PyTorch, CUDA, NCCL)

**Key features:**
- **GPU variety**: B200, H100, GH200, A100, A10, A6000, V100
- **Lambda Stack**: Pre-installed PyTorch, TensorFlow, CUDA, cuDNN, NCCL
- **Persistent filesystems**: Keep data across instance restarts
- **1-Click Clusters**: 16-512 GPU Slurm clusters with InfiniBand
- **Simple pricing**: Pay-per-minute, no egress fees
- **Global regions**: 12+ regions worldwide

**Do NOT use Lambda Labs when** — use these alternatives instead:
- **Modal**: For serverless, auto-scaling workloads
- **SkyPilot**: For multi-cloud orchestration and cost optimization
- **RunPod**: For cheaper spot instances and serverless endpoints
- **Vast.ai**: For GPU marketplace with lowest prices

Also a poor fit for bursty/idle-heavy inference: there is no auto-stop, so an
idle instance bills until you terminate it.

## Routing table

| To do this | Read |
|---|---|
| Set up an account, pick a GPU type/shape, read prices and launch times, right-size for cost | `references/instances-and-pricing.md` |
| SSH in, manage keys, tunnel Jupyter/TensorBoard, verify the Lambda Stack, understand firewall/bandwidth/private IPs | `references/access-and-environment.md` |
| Launch/list/terminate instances or manage SSH keys programmatically (Python client or curl) | `references/api-and-cli.md` |
| Decide what to store where: create/attach persistent filesystems, ephemeral root volume rules | `references/storage-and-filesystems.md` |
| Run training: single-GPU, single-node DDP, checkpointing, 1-Click Cluster multi-node, LLM fine-tuning, batch inference | `references/training-workflows.md` |
| Multi-node FSDP/DeepSpeed, Slurm `sbatch`, API automation, conda/Docker envs, W&B/TensorBoard, idle auto-terminate, security | `references/advanced-usage.md` |
| Diagnose launch, SSH, GPU, filesystem, network, package, training, or billing failures | `references/troubleshooting.md` |

## Key constraints and gotchas

- **Add your SSH key before launching.** Keys added afterwards are not injected
  into a running instance.
- **Filesystems attach only at launch time.** Forgot one? Terminate and relaunch.
- **`/home/ubuntu` is ephemeral.** `ubuntu` is the default login user; its home is
  fast local NVMe on the root volume and is destroyed on termination. Persistent
  data belongs in `/lambda/nfs/<FILESYSTEM_NAME>`.
- **No stop state.** Instances are running (billed per minute) or terminated. No
  auto-stop — terminate idle instances yourself.
- **Region locality matters.** Filesystem and instance must be in the same region;
  multi-node jobs require all nodes in one region.
- **Only port 22 is open by default.** Use SSH tunnels or open ports in the console.
- Launching takes 3-5 min (single-GPU) or 10-15 min (multi-GPU) — SSH refusals
  before that are expected.
- Don't modify the system Python; use a venv/conda env.

| Issue | Solution |
|-------|----------|
| Instance won't launch | Check region availability, try different GPU |
| SSH connection refused | Wait for instance to initialize (3-15 min) |
| Data lost after terminate | Use persistent filesystems |
| Slow data transfer | Use filesystem in same region |
| GPU not detected | Reboot instance, check drivers |

## End-to-end skeleton

```bash
# 1. Launch (console, or API - see references/api-and-cli.md)
#    GPU type + region + SSH key + filesystem, all chosen at launch

# 2. Connect (ubuntu is the default login user)
ssh ubuntu@<INSTANCE-IP>

# 3. Verify the stack
nvidia-smi && python -c "import torch; print(torch.cuda.is_available())"

# 4. Train, writing checkpoints to the PERSISTENT filesystem
torchrun --nproc_per_node=8 train_ddp.py \
  --checkpoint-dir /lambda/nfs/my-storage/checkpoints

# 5. Terminate - billing stops only here, and /home/ubuntu is wiped
curl -u $LAMBDA_API_KEY: \
  -X POST https://cloud.lambdalabs.com/api/v1/instance-operations/terminate \
  -H "Content-Type: application/json" \
  -d '{"instance_ids": ["<INSTANCE-ID>"]}' | jq
```

## Resources

- **Documentation**: https://docs.lambda.ai
- **Console**: https://cloud.lambda.ai
- **Pricing**: https://lambda.ai/instances
- **Support**: https://support.lambdalabs.com
- **Blog**: https://lambda.ai/blog
