# SAE Concepts, Metrics and Hyperparameter Ranges

Explains why SAEs exist (superposition/polysemanticity), what they optimize, which architecture to pick, and the numeric targets and hyperparameter ranges used when training.

## The problem: polysemanticity & superposition

Individual neurons in neural networks are **polysemantic** - they activate in multiple, semantically distinct contexts. This happens because models use **superposition** to represent more features than they have neurons, making interpretability difficult.

**SAEs solve this** by decomposing dense activations into sparse, monosemantic features - typically only a small number of features activate for any given input, and each feature corresponds to an interpretable concept.

## What SAEs learn

SAEs are trained to reconstruct model activations through a sparse bottleneck:

```
Input Activation → Encoder → Sparse Features → Decoder → Reconstructed Activation
    (d_model)       ↓        (d_sae >> d_model)    ↓         (d_model)
                 sparsity                      reconstruction
                 penalty                          loss
```

**Loss function**: `MSE(original, reconstructed) + L1_coefficient × L1(features)`

## Key validation (Anthropic research)

In "Towards Monosemanticity", human evaluators found **70% of SAE features genuinely interpretable**. Features discovered include:

- DNA sequences, legal language, HTTP requests
- Hebrew text, nutrition statements, code syntax
- Sentiment, named entities, grammatical structures

## Evaluation metrics

| Metric | Target | Meaning |
|--------|--------|---------|
| **L0** | 50-200 | Average active features per token |
| **CE Loss Score** | 80-95% | Cross-entropy recovered vs original |
| **Dead Features** | <5% | Features that never activate |
| **Explained Variance** | >90% | Reconstruction quality |

## Key hyperparameters

| Parameter | Typical Value | Effect |
|-----------|---------------|--------|
| `d_sae` | 4-16× d_model | More features, higher capacity |
| `l1_coefficient` | 5e-5 to 1e-4 | Higher = sparser, less accurate |
| `lr` | 1e-4 to 1e-3 | Standard optimizer LR |
| `l1_warm_up_steps` | 500-2000 | Prevents early feature death |

## Choosing an SAE architecture

| Architecture | Description | Use Case |
|--------------|-------------|----------|
| **Standard** | ReLU + L1 penalty | General purpose |
| **Gated** | Learned gating mechanism | Better sparsity control |
| **TopK** | Exactly K active features | Consistent sparsity |

```python
# TopK SAE (exactly 50 features active)
cfg = LanguageModelSAERunnerConfig(
    architecture="topk",
    activation_fn="topk",
    activation_fn_kwargs={"k": 50},
)
```

See `api.md` for the JumpReLU architecture and the full parameter reference.

## When to use SAELens vs alternatives

**Use SAELens when you need to:**
- Discover interpretable features in model activations
- Understand what concepts a model has learned
- Study superposition and feature geometry
- Perform feature-based steering or ablation
- Analyze safety-relevant features (deception, bias, harmful content)

**Consider alternatives when:**
- You need basic activation analysis → Use **TransformerLens** directly
- You want causal intervention experiments → Use **pyvene** or **TransformerLens**
- You need production steering → Consider direct activation engineering
