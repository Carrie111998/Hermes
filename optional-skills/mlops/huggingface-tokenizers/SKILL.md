---
name: huggingface-tokenizers
description: 'HuggingFace Tokenizers: Rust tokenizer training and fast encoding - build BPE/WordPiece/Unigram vocabularies from scratch, offset alignment, 1GB in under 20s.'
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [tokenizers, transformers, datasets]
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Tokenization, HuggingFace, BPE, WordPiece, Unigram, Fast Tokenization, Rust, Custom Tokenizer, Alignment Tracking, Production]

---

# HuggingFace Tokenizers - Fast Tokenization for NLP

Fast, production-ready tokenizers with Rust performance and Python ease-of-use.

## When to use / when NOT to use

Use this library when you need to **train a tokenizer from scratch** (BPE, WordPiece,
Unigram), tokenize large corpora fast (<20s per GB on CPU), or track offsets from tokens
back to character positions in the original text.

Do NOT use it when you only need to load a pretrained tokenizer for inference — reach for
`transformers.AutoTokenizer` (it wraps this library anyway). Use **SentencePiece** for the
T5/ALBERT-style language-independent trainer, and **tiktoken** for OpenAI GPT BPE vocabs.

## Routing table

| To do X | Read |
|---------|------|
| Train a custom tokenizer end-to-end: data prep, trainer parameters, large/streaming datasets, domain-specific (code/medical/multilingual) tokenizers, vocab-size selection, quality tests | `references/training.md` |
| Understand or choose between BPE, WordPiece and Unigram: worked examples, scoring, Viterbi, byte-level variants, compression comparison, model families | `references/algorithms.md` |
| Configure pipeline components: normalizers, pre-tokenizers, models, post-processors, decoders, alignment/offset tracking, full BERT/GPT-2/T5 pipelines | `references/pipeline.md` |
| Use a tokenizer with `transformers`: AutoTokenizer, PreTrainedTokenizerFast, special tokens, padding/truncation/stride, word_ids, chat templates | `references/integration.md` |
| Speed/memory benchmarks, batch + parallel encoding, list of pretrained tokenizers on the Hub | `references/performance-and-models.md` |

## Key constraints

- Install with `pip install tokenizers` (add `transformers` for the AutoTokenizer wrapper).
- The pipeline order is fixed: **normalization -> pre-tokenization -> model -> post-processing**;
  a missing pre-tokenizer silently produces terrible merges.
- `special_tokens` must be passed to the *trainer*, otherwise their IDs are unstable.
- Padding and truncation are stateful on the tokenizer object (`enable_padding` /
  `enable_truncation`), not per-call arguments.
- Offsets are only available from fast (Rust) tokenizers — check `tokenizer.is_fast`.
- Vocabulary and merges are immutable after training; retrain to change them.

## End-to-end skeleton

```python
from tokenizers import Tokenizer
from tokenizers.models import BPE
from tokenizers.trainers import BpeTrainer
from tokenizers.pre_tokenizers import Whitespace

# 1. Build and train
tokenizer = Tokenizer(BPE(unk_token="[UNK]"))
tokenizer.pre_tokenizer = Whitespace()
trainer = BpeTrainer(
    vocab_size=30000,
    special_tokens=["[UNK]", "[CLS]", "[SEP]", "[PAD]", "[MASK]"],
    min_frequency=2,
)
tokenizer.train(["train.txt", "validation.txt"], trainer)
tokenizer.save("my-tokenizer.json")

# 2. Encode
output = tokenizer.encode("Hello, how are you?")
print(output.tokens, output.ids, output.offsets)

# 3. Decode
print(tokenizer.decode(output.ids))

# Loading a pretrained vocabulary instead of training:
# tokenizer = Tokenizer.from_pretrained("bert-base-uncased")
```
