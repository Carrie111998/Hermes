# Lambda Labs Instances and Pricing

Account setup, GPU types with prices, instance shapes, launch times, and how to pick the cheapest GPU that fits the workload.

## Account setup

1. Create account at https://lambda.ai
2. Add payment method
3. Generate API key from dashboard
4. Add SSH key (required **before** launching instances)

## Launch via console

1. Go to https://cloud.lambda.ai/instances
2. Click "Launch instance"
3. Select GPU type and region
4. Choose SSH key
5. Optionally attach filesystem (only possible at launch time)
6. Launch and wait 3-15 minutes

## Available GPUs

| GPU | VRAM | Price/GPU/hr | Best For |
|-----|------|--------------|----------|
| B200 SXM6 | 180 GB | $4.99 | Largest models, fastest training |
| H100 SXM | 80 GB | $2.99-3.29 | Large model training |
| H100 PCIe | 80 GB | $2.49 | Cost-effective H100 |
| GH200 | 96 GB | $1.49 | Single-GPU large models |
| A100 80GB | 80 GB | $1.79 | Production training |
| A100 40GB | 40 GB | $1.29 | Standard training |
| A10 | 24 GB | $0.75 | Inference, fine-tuning |
| A6000 | 48 GB | $0.80 | Good VRAM/price ratio |
| V100 | 16 GB | $0.55 | Budget training |

## Instance configurations

```
8x GPU: Best for distributed training (DDP, FSDP)
4x GPU: Large models, multi-GPU training
2x GPU: Medium workloads
1x GPU: Fine-tuning, inference, development
```

## Launch times

- Single-GPU: 3-5 minutes
- Multi-GPU: 10-15 minutes

## Cost optimization

### Choose right GPU

| Task | Recommended GPU |
|------|-----------------|
| LLM fine-tuning (7B) | A100 40GB |
| LLM fine-tuning (70B) | 8x H100 |
| Inference | A10, A6000 |
| Development | V100, A10 |
| Maximum performance | B200 |

### Reduce costs

1. **Use filesystems**: Avoid re-downloading data
2. **Checkpoint frequently**: Resume interrupted training
3. **Right-size**: Don't over-provision GPUs
4. **Terminate idle**: No auto-stop exists — you must manually terminate

Billing is per minute. There is no "stop" state: an instance is either running
(billed) or terminated (root volume destroyed).

### Monitor usage

- Dashboard shows real-time GPU utilization
- API for programmatic monitoring

See `advanced-usage.md` for per-workload instance selection tables and an
auto-terminate-idle script.
