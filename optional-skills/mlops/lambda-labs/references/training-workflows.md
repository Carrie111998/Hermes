# Lambda Labs Training Workflows

End-to-end recipes on a launched instance: single-GPU training, single-node DDP, checkpointing to persistent storage, 1-Click Cluster multi-node jobs, LLM fine-tuning, and batch inference.

## Single-GPU training

```bash
# SSH to instance
ssh ubuntu@<IP>

# Clone repo
git clone https://github.com/user/project
cd project

# Install dependencies
pip install -r requirements.txt

# Train
python train.py --epochs 100 --checkpoint-dir /lambda/nfs/storage/checkpoints
```

## Multi-GPU training (single node)

```python
# train_ddp.py
import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP

def main():
    dist.init_process_group("nccl")
    rank = dist.get_rank()
    device = rank % torch.cuda.device_count()

    model = MyModel().to(device)
    model = DDP(model, device_ids=[device])

    # Training loop...

if __name__ == "__main__":
    main()
```

```bash
# Launch with torchrun (8 GPUs)
torchrun --nproc_per_node=8 train_ddp.py
```

## Checkpoint to filesystem

```python
import os

checkpoint_dir = "/lambda/nfs/my-storage/checkpoints"
os.makedirs(checkpoint_dir, exist_ok=True)

# Save checkpoint
torch.save({
    'epoch': epoch,
    'model_state_dict': model.state_dict(),
    'optimizer_state_dict': optimizer.state_dict(),
    'loss': loss,
}, f"{checkpoint_dir}/checkpoint_{epoch}.pt")
```

## 1-Click Clusters

### Overview

High-performance Slurm clusters with:
- 16-512 NVIDIA H100 or B200 GPUs
- NVIDIA Quantum-2 400 Gb/s InfiniBand
- GPUDirect RDMA at 3200 Gb/s
- Pre-installed distributed ML stack

### Included software

- Ubuntu 22.04 LTS + Lambda Stack
- NCCL, Open MPI
- PyTorch with DDP and FSDP
- TensorFlow
- OFED drivers

### Storage

- 24 TB NVMe per compute node (ephemeral)
- Lambda filesystems for persistent data

### Multi-node training

```bash
# On Slurm cluster
srun --nodes=4 --ntasks-per-node=8 --gpus-per-node=8 \
  torchrun --nnodes=4 --nproc_per_node=8 \
  --rdzv_backend=c10d --rdzv_endpoint=$MASTER_ADDR:29500 \
  train.py
```

See `advanced-usage.md` for `sbatch` scripts, interactive cluster sessions, FSDP
and DeepSpeed ZeRO configs, and manual multi-node launches without Slurm.

## Workflow 1: Fine-tuning an LLM

```bash
# 1. Launch 8x H100 instance with filesystem

# 2. SSH and setup
ssh ubuntu@<IP>
pip install transformers accelerate peft

# 3. Download model to filesystem
python -c "
from transformers import AutoModelForCausalLM
model = AutoModelForCausalLM.from_pretrained('<model>')
model.save_pretrained('/lambda/nfs/storage/models/llama-2-7b')
"

# 4. Fine-tune with checkpoints on filesystem
accelerate launch --num_processes 8 train.py \
  --model_path /lambda/nfs/storage/models/llama-2-7b \
  --output_dir /lambda/nfs/storage/outputs \
  --checkpoint_dir /lambda/nfs/storage/checkpoints
```

Download models to the persistent filesystem, not to `/home/ubuntu` — the root
volume is ephemeral and you would re-download on every relaunch.

## Workflow 2: Batch inference

```bash
# 1. Launch A10 instance (cost-effective for inference)

# 2. Run inference
python inference.py \
  --model /lambda/nfs/storage/models/fine-tuned \
  --input /lambda/nfs/storage/data/inputs.jsonl \
  --output /lambda/nfs/storage/data/outputs.jsonl
```
