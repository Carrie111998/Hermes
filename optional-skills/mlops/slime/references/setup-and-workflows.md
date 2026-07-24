# slime Setup and Training Workflows

Purpose: installation options plus the three end-to-end training workflows (synchronous GRPO, asynchronous rollout, multi-turn agentic) with their data formats and launch commands.

> **Upstream-repo paths**: `scripts/models/*.sh`, `examples/`, `train.py` and `train_async.py` live inside the cloned upstream slime repo (https://github.com/THUDM/slime), not in this skill directory. Run them from the repo root after `git clone`, or from `/root/slime` inside the Docker image.

## Installation

### Recommended: Docker

```bash
docker pull slimerl/slime:latest
docker run --rm --gpus all --ipc=host --shm-size=16g \
  -it slimerl/slime:latest /bin/bash

# Inside container
cd /root/slime && pip install -e . --no-deps
```

### From Source

```bash
git clone https://github.com/THUDM/slime.git
cd slime
pip install -r requirements.txt
pip install -e .
```

---

## Workflow 1: Standard (Synchronous) GRPO Training

Use this workflow for training reasoning models with group-relative advantages.

### Prerequisites Checklist
- [ ] Docker environment or Megatron-LM + SGLang installed
- [ ] Model checkpoint (HuggingFace or Megatron format)
- [ ] Training data in JSONL format

### Step 1: Prepare Data

```python
# data.jsonl format
{"prompt": "What is 2 + 2?", "label": "4"}
{"prompt": "Solve: 3x = 12", "label": "x = 4"}
```

Or with chat format:
```python
{
    "prompt": [
        {"role": "system", "content": "You are a math tutor."},
        {"role": "user", "content": "What is 15 + 27?"}
    ],
    "label": "42"
}
```

### Step 2: Configure Model

Choose a pre-configured model script (inside the cloned upstream repo):

```bash
# List available models
ls scripts/models/
# glm4-9B.sh, qwen3-4B.sh, qwen3-30B-A3B.sh, deepseek-v3.sh, llama3-8B.sh, ...

# Source your model
source scripts/models/qwen3-4B.sh
```

This sets the `MODEL_ARGS` and `CKPT_ARGS` bash arrays. See
[api-reference.md](api-reference.md) for the contents of an example model script.

### Step 3: Launch Training

```bash
python train.py \
    --actor-num-nodes 1 \
    --actor-num-gpus-per-node 8 \
    --rollout-num-gpus 8 \
    --advantage-estimator grpo \
    --use-kl-loss \
    --kl-loss-coef 0.001 \
    --prompt-data /path/to/train.jsonl \
    --input-key prompt \
    --label-key label \
    --apply-chat-template \
    --rollout-batch-size 32 \
    --n-samples-per-prompt 8 \
    --global-batch-size 256 \
    --num-rollout 3000 \
    --save-interval 100 \
    --eval-interval 50 \
    ${MODEL_ARGS[@]}
```

A minimal 4-GPU variant:

```bash
source scripts/models/qwen3-4B.sh

python train.py \
    --actor-num-nodes 1 \
    --actor-num-gpus-per-node 4 \
    --rollout-num-gpus 4 \
    --advantage-estimator grpo \
    --use-kl-loss --kl-loss-coef 0.001 \
    --rollout-batch-size 32 \
    --n-samples-per-prompt 8 \
    --global-batch-size 256 \
    --num-rollout 3000 \
    --prompt-data /path/to/data.jsonl \
    ${MODEL_ARGS[@]} ${CKPT_ARGS[@]}
```

### Step 4: Monitor Training
- [ ] Check TensorBoard: `tensorboard --logdir outputs/`
- [ ] Verify reward curves are increasing
- [ ] Monitor GPU utilization across nodes

---

## Workflow 2: Asynchronous Training

Use async mode for higher throughput by overlapping rollout and training.

### When to Use Async
- Large models with long generation times
- High GPU idle time in synchronous mode
- Sufficient memory for buffering

### Launch Async Training

```bash
python train_async.py \
    --actor-num-nodes 1 \
    --actor-num-gpus-per-node 8 \
    --rollout-num-gpus 8 \
    --advantage-estimator grpo \
    --async-buffer-size 4 \
    --prompt-data /path/to/train.jsonl \
    ${MODEL_ARGS[@]}
```

### Async-Specific Parameters

```bash
--async-buffer-size 4        # Number of rollouts to buffer
--update-weights-interval 2  # Sync weights every N rollouts
```

**Note**: `--colocate` is NOT supported with async training — rollout and training
must own separate GPUs.

---

## Workflow 3: Multi-Turn Agentic Training

Use this workflow for training agents with tool use or multi-step reasoning.

### Prerequisites
- [ ] Custom generate function for multi-turn logic
- [ ] Tool/environment interface

### Step 1: Define Custom Generate Function

```python
# custom_generate.py
async def custom_generate(args, samples, evaluation=False):
    """Multi-turn generation with tool calling."""
    for sample in samples:
        conversation = sample.prompt

        for turn in range(args.max_turns):
            # Generate response
            response = await generate_single(conversation)

            # Check for tool call
            tool_call = extract_tool_call(response)
            if tool_call:
                tool_result = execute_tool(tool_call)
                conversation.append({"role": "assistant", "content": response})
                conversation.append({"role": "tool", "content": tool_result})
            else:
                break

        sample.response = response
        sample.reward = compute_reward(sample)

    return samples
```

The fully-typed version of this signature, including `loss_mask` construction, is in
[api-reference.md](api-reference.md).

### Step 2: Launch with Custom Function

```bash
python train.py \
    --custom-generate-function-path custom_generate.py \
    --max-turns 5 \
    --prompt-data /path/to/agent_data.jsonl \
    ${MODEL_ARGS[@]}
```

See `examples/search-r1/` in the cloned upstream repo for a complete multi-turn search
example; `examples/` holds 14+ worked examples in total.

---

## Co-location Mode

Share GPUs between training and inference to reduce total GPU count:

```bash
python train.py \
    --colocate \
    --actor-num-gpus-per-node 8 \
    --sglang-mem-fraction-static 0.4 \
    ${MODEL_ARGS[@]}
```

Lower `--sglang-mem-fraction-static` when colocated, since the training process needs
the remaining device memory.
