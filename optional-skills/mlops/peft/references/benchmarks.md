# PEFT Benchmarks

Purpose: reference numbers for memory, throughput, and quality cost of LoRA/QLoRA/IA3 versus full fine-tuning.

## Memory usage (Llama 3.1 8B)

| Method | GPU Memory | Trainable Params |
|--------|-----------|------------------|
| Full fine-tuning | 60+ GB | 8B (100%) |
| LoRA r=16 | 18 GB | 14M (0.17%) |
| QLoRA r=16 | 6 GB | 14M (0.17%) |
| IA3 | 16 GB | 800K (0.01%) |

## Training speed (A100 80GB)

| Method | Tokens/sec | vs Full FT |
|--------|-----------|------------|
| Full FT | 2,500 | 1x |
| LoRA | 3,200 | 1.3x |
| QLoRA | 2,100 | 0.84x |

QLoRA trades throughput for memory: dequantization overhead makes it slower than plain
LoRA, but it is the only option that fits a 70B model on a single 24GB GPU.

## Quality (MMLU benchmark)

| Model | Full FT | LoRA | QLoRA |
|-------|---------|------|-------|
| Llama 2-7B | 45.3 | 44.8 | 44.1 |
| Llama 2-13B | 54.8 | 54.2 | 53.5 |

Roughly a 0.5-point MMLU cost for LoRA and ~1.2 points for QLoRA versus full fine-tuning —
the "~5% quality trade-off" rule of thumb.
