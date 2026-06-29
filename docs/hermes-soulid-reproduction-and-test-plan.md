# Soul ID 复现与测试计划

状态：实验计划，不是生产事实
来源：

- `docs/notion-source/hermes/pages/02-测试实际模型.md`
- `docs/notion-source/hermes/pages/03-复现方式.md`

## 用途

This plan captures how to test or approximate Soul ID behavior in an open-source Hermes implementation. It must not be read as proof that Higgsfield uses a specific base model or training architecture.

## 需要验证什么

| 问题 | 为什么重要 |
|---|---|
| Does the model preserve identity across angle, lighting, expression, and scene changes? | Determines whether the asset is usable for production workflows. |
| Does long prompt detail survive near the end of the prompt? | Helps infer text encoder/context behavior. |
| Does text rendering and symbol control work? | Helps differentiate older CLIP-bound models from newer text encoders. |
| Does geometry/counting hold under complex layouts? | Helps identify U-Net vs DiT-style behavior, but remains heuristic. |
| What is TTFT/latency after warm start? | Helps estimate distilled vs heavier model routes. |

## 测试集合

### 文本编码边界

- Long prompt with identity reference near the beginning and small scene constraints in the last 20 percent.
- Prompt with exact strings such as `1234-XYZ` and font/layout constraints.
- Cross-language prompt mixing Chinese, English, digits, and symbols.

### Spatial and Geometry Boundary

- Exact object counts.
- Overlapping objects.
- Extreme low-angle or high-angle identity shots.
- Half-shadow face lighting.
- Full-body character plus prop/environment consistency.

### Operational Signals

- First output latency after warm start.
- Number of images generated per batch.
- Status transitions for identity training.
- Error states for low-quality or insufficient inputs.
- API/network payload shape if developer access exists.

## 开源复现选项

| Option | Fit | Notes |
|---|---|---|
| Flux LoRA + PuLID | Best current approximation | Combines trained character adaptation with identity injection. |
| Flux LoRA only | Strong and simpler | Good consistency, weaker for extreme angles than hybrid route. |
| PuLID Flux route | Fastest near one-click path | Less training burden, but consistency ceiling may be lower. |
| InstantID | Mature quick baseline | Useful for validation, not likely top quality. |
| SDXL LoRA | Simple baseline | Good for comparison, likely weaker for text/spatial fidelity. |

## 推荐实验路径

1. Build a small benchmark set with 10 to 30 clean identity images.
2. Train a Flux LoRA baseline.
3. Add PuLID at generation time and compare with LoRA-only.
4. Add InstantID and SDXL LoRA as lower-cost baselines.
5. Score outputs on identity similarity, prompt fidelity, geometry, speed, failure rate, and user correction burden.

## 评估量表

| Metric | Pass condition |
|---|---|
| Identity consistency | Same person remains recognizable across at least 8 of 10 varied prompts. |
| Prompt fidelity | Critical prompt constraints are visible in at least 7 of 10 prompts. |
| Geometry | Counts and spatial relations pass at least 6 of 10 stress prompts. |
| Latency | Warm generation stays within the chosen product SLA. |
| Failure visibility | Low-quality inputs and missing assets return visible errors. |

## 暂时不能宣称的内容

- Do not claim the production Soul ID backend is LoRA unless verified.
- Do not claim exact base model lineage from prompt tests alone.
- Do not hardcode `text2image_soul_v2` into core Hermes; keep it as provider/model adapter metadata.
- Do not treat one successful face prompt as proof of stable identity asset behavior.
