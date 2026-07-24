# TRL Troubleshooting

Purpose: symptom-to-fix list for the trainer-level failures seen most often across DPO, reward modeling, and PPO runs.

## Issue: OOM during DPO training

DPO holds a frozen reference model in addition to the policy, so it needs roughly twice
the memory of SFT. Reduce batch size and sequence length:

```python
config = DPOConfig(
    per_device_train_batch_size=1,  # Reduce from 4
    max_length=512,  # Reduce from 1024
    gradient_accumulation_steps=8  # Maintain effective batch
)
```

Or use gradient checkpointing:
```python
model.gradient_checkpointing_enable()
```

## Issue: Poor alignment quality

Tune the beta parameter:
```python
# Higher beta = more conservative (stays closer to reference)
config = DPOConfig(beta=0.5)  # Default 0.1

# Lower beta = more aggressive alignment
config = DPOConfig(beta=0.01)
```

If beta tuning is not enough, try a different loss variant (IPO, RPO, cDPO, APO) — see
[dpo-variants.md](dpo-variants.md).

## Issue: Reward model not learning

Check loss type and learning rate:
```python
config = RewardConfig(
    learning_rate=1e-5,  # Try different LR
    num_train_epochs=3  # Train longer
)
```

Ensure the preference dataset has clear winners:
```python
# Verify dataset
print(dataset[0])
# Should have clear chosen > rejected
```

## Issue: PPO training unstable

Adjust the KL coefficient and clip range:
```python
config = PPOConfig(
    kl_coef=0.1,  # Increase from 0.05
    cliprange=0.1  # Reduce from 0.2
)
```

## Issue: GRPO loss rising / mode collapse

Rising loss is the *expected* GRPO pattern, not a bug. Mode collapse (all completions
identical) and reward-hacking diagnostics are covered in
[grpo-training.md](grpo-training.md).
