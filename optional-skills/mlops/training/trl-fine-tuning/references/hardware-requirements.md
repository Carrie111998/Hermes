# TRL Hardware Requirements

Purpose: VRAM budgets per training method, multi-GPU setup, and the memory-optimization levers to reach for first.

## Requirements

- **GPU**: NVIDIA (CUDA required)
- **VRAM**: depends on model and method
  - SFT 7B: 16GB (with LoRA)
  - DPO 7B: 24GB (stores reference model)
  - PPO 7B: 40GB (policy + reward model)
  - GRPO 7B: 24GB (more memory efficient)
- **Multi-GPU**: supported via `accelerate` (`accelerate launch train.py`)
- **Mixed precision**: BF16 recommended (A100/H100)

PPO is the heaviest because it holds policy, reference, value head, and reward model
simultaneously. GRPO drops the value model, which is where its savings come from — see the
memory comparison table in [online-rl.md](online-rl.md).

## Memory optimization

- Use LoRA/QLoRA for all methods (pass `peft_config=` to any TRL trainer)
- Enable gradient checkpointing
- Use smaller batch sizes with gradient accumulation to keep the effective batch constant
- Reduce `max_length` / `max_prompt_length` before reducing batch size if sequences are long

Multi-GPU launch details for SFT are in [sft-training.md](sft-training.md).
