---
name: nemo-curator
description: GPU-accelerated data curation for LLM training. Supports text/image/video/audio. Features fuzzy deduplication (16× faster), quality filtering (30+ heuristics), semantic deduplication, PII redaction, NSFW detection. Scales across GPUs with RAPIDS. Use for preparing high-quality training datasets, cleaning web data, or deduplicating large corpora.
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [nemo-curator, cudf, dask, rapids]
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Data Processing, NeMo Curator, Data Curation, GPU Acceleration, Deduplication, Quality Filtering, NVIDIA, RAPIDS, PII Redaction, Multimodal, LLM Training Data]

---

# NeMo Curator - GPU-Accelerated Data Curation

NVIDIA's toolkit for preparing high-quality training data for LLMs, built on RAPIDS/Dask.

## When to use this skill

**Use NeMo Curator when:**
- Preparing LLM training data from web scrapes (Common Crawl)
- You need fast deduplication (16× faster than CPU)
- Curating multi-modal datasets (text, images, video, audio)
- Filtering low-quality or toxic content, or redacting PII
- Scaling data processing across a GPU cluster

**Do NOT use it when:**
- No GPUs available and the dataset is small → **datatrove** (CPU-based, open source)
- You want Allen AI's curation conventions → **dolma**
- You need general ML data processing without curation semantics → **Ray Data**

## Routing table

| To do this | Read |
|------------|------|
| Pick and tune quality filters (30+ heuristics, language ID, quality/NSFW classifiers, ordering) | [references/filtering.md](references/filtering.md) |
| Choose and configure exact / fuzzy (MinHash+LSH) / semantic dedup, with parameter ranges and recall trade-offs | [references/deduplication.md](references/deduplication.md) |
| Compose stages end to end: PII redaction, classifier stage, full Common Crawl pipeline, Dask/GPU cluster setup, I/O formats | [references/pipeline.md](references/pipeline.md) |
| Curate images, video, or audio (aesthetic/NSFW, CLIP, scene detection, ASR, WER) | [references/multimodal.md](references/multimodal.md) |
| Justify GPU vs CPU, cite benchmarks, or estimate cloud cost | [references/performance.md](references/performance.md) |

## Installation

```bash
# Text curation (CUDA 12)
uv pip install "nemo-curator[text_cuda12]"

# All modalities
uv pip install "nemo-curator[all_cuda12]"

# CPU-only (slower)
uv pip install "nemo-curator[cpu]"
```

## Key constraints and gotchas

- **Order stages cheap → expensive.** Heuristic filters first, dedup next, GPU classifiers last; running a DeBERTa quality classifier over undeduplicated data wastes most of the compute.
- **Exact before fuzzy.** Exact dedup is nearly free and shrinks the input to the expensive MinHash/LSH stage.
- **Fuzzy dedup parameters trade recall for speed**: `num_hashes` 128-512, `num_buckets` 10-50, `jaccard_threshold` 0.7-0.9. Fewer buckets = more recall, slower.
- **Semantic dedup is slow (~90% recall)** — reserve it for high-value corpora, not raw web scrape.
- **GPU acceleration is effectively required** for the 10-16× numbers; the `[cpu]` extra works but changes the cost calculus entirely.
- **Tune thresholds on a 10k-document sample** before a full run.
- **Prefer Parquet output**; JSONL is supported, multi-modal uses WebDataset TAR.

## End-to-end skeleton

```python
from nemo_curator import get_client
from nemo_curator.datasets import DocumentDataset
from nemo_curator.filters import WordCountFilter, LanguageIdentificationFilter
from nemo_curator.modules import ExactDuplicates, FuzzyDuplicates, Modify
from nemo_curator.modifiers import PIIRedactor

client = get_client(cluster_type="gpu", n_workers=8)

dataset = DocumentDataset.read_parquet("common_crawl/*.parquet")

# 1. cheap heuristics
dataset = dataset.filter(WordCountFilter(min_words=100, max_words=50000))
dataset = dataset.filter(LanguageIdentificationFilter(target_languages=["en"]))

# 2. dedup: exact then fuzzy
dataset = ExactDuplicates(id_field="id", text_field="text")(dataset)
dataset = FuzzyDuplicates(
    id_field="id", text_field="text",
    num_hashes=260, num_buckets=20, jaccard_threshold=0.8,
)(dataset)

# 3. PII
dataset = Modify(PIIRedactor())(dataset)

dataset.to_parquet("curated_common_crawl/")
client.close()
```

## Resources

- **GitHub**: https://github.com/NVIDIA/NeMo-Curator
- **Docs**: https://docs.nvidia.com/nemo-framework/user-guide/latest/datacuration/
- **Version**: 0.4.0+ · **License**: Apache 2.0
