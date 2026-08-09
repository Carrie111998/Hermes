"""Isolated lexical vs live BGE-M3 dense/RRF benchmark.

The benchmark owns a temporary SQLite database and does not call production
hybrid retrieval. Live HTTP execution is opt-in only.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence

from plugins.semantic_graph.embedding import EmbeddingModelIdentity, serialize_embedding_node, source_text_hash
from plugins.semantic_graph.fusion import reciprocal_rank_fusion
from plugins.semantic_graph.retrieval import render_context, search_and_rank
from plugins.semantic_graph.sanitize import normalize_text, sanitize_text
from plugins.semantic_graph.store import SemanticGraphStore

FIXTURE = Path(__file__).parents[1] / "tests" / "fixtures" / "semantic_graph_retrieval_benchmark.json"
CONTROL_CODE_REVISION = "0c89ef566343dbed810cedff81c9ac405febf0da"
CONTROL = {
    "repo": "KGESH/nsfw-bge-m3",
    "family": "BGE-M3 fine-tune",
    "hf_revision": "e22b93e36704360fc712c8894de59a66cdb1638e",
    "gguf_sha256": "ecc1d6cd89a82ab72ced141e63d4c5b6644f1723c94d41e3d36ce40a52a21f16",
    "dimensions": 1024,
    "serializer_profile": "bge_m3_control",
}
BENCHMARK_IDENTITY = EmbeddingModelIdentity(
    provider="benchmark",
    model="KGESH/nsfw-bge-m3",
    revision=CONTROL["hf_revision"],
    dimensions=1024,
    serializer_version=1,
)
NAMESPACE = BENCHMARK_IDENTITY.namespace
TOP_K = 8
LEXICAL_CANDIDATES = 30
DENSE_CANDIDATES = 30
RRF_K = 60


def benchmark_code_revision() -> str:
    """Resolve the exact checked-out benchmark revision at execution time."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "uncommitted-or-unavailable"


def load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def serialize_bge_query(query: str) -> str:
    """Raw BGE-M3 query serializer; deliberately no Qwen instruction."""
    cleaned = normalize_text(sanitize_text(query, max_chars=4001).text)[:4000]
    if not cleaned:
        raise ValueError("query must not be empty")
    return cleaned


def make_store(db_path: Path) -> tuple[SemanticGraphStore, str, str]:
    fixture = load_fixture()
    store = SemanticGraphStore(db_path)
    store.ensure_ready()
    run_a = store.create_run(objective="isolated BGE-M3 A/B benchmark")["run_id"]
    run_b = store.create_run(objective="isolated BGE-M3 decoy run")["run_id"]

    for item in fixture["nodes"]:
        store.upsert_node({
            "node_id": item["identity"], "node_type": "Preference",
            "subtype": item["identity"].split(".")[0], "label": item["label"],
            "normalized_label": item["label"].casefold(), "summary": item["summary"],
            "identity_key": item["identity"], "status": "asserted", "authority": "user",
            "confidence": 0.95, "salience": 0.8,
        })
        store.link_run_node(run_a, item["identity"])

    for item in fixture["correction_nodes"]:
        store.upsert_node({
            "node_id": item["old_identity"], "node_type": "Preference",
            "subtype": "correction", "label": f"Historical preference {item['old_identity']}",
            "normalized_label": item["old_identity"], "summary": item["old_summary"],
            "identity_key": item["old_identity"], "status": "superseded", "authority": "assistant",
            "confidence": 0.95, "salience": 0.8,
        })
        store.upsert_node({
            "node_id": item["new_identity"], "node_type": "Preference",
            "subtype": "correction", "label": f"Current preference {item['new_identity']}",
            "normalized_label": item["new_identity"], "summary": item["new_summary"],
            "identity_key": item["new_identity"], "status": "asserted", "authority": "user",
            "confidence": 0.95, "salience": 0.8,
        })
        store.link_run_node(run_a, item["new_identity"])

    store.upsert_node({
        "node_id": "run-b-only", "node_type": "Preference", "subtype": "isolation",
        "label": "TypeScript in another run", "normalized_label": "typescript in another run",
        "summary": "This decoy must not cross run scope.", "identity_key": "run-b-only",
        "status": "asserted", "authority": "user", "confidence": 0.99, "salience": 0.99,
    })
    store.link_run_node(run_b, "run-b-only")
    return store, run_a, run_b


class HttpEmbeddingClient:
    """Restricted stdlib client: only POST /v1/embeddings is used."""

    def __init__(self, base_url: str, model: str = "nsfw-bge-m3-v4.gguf") -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model

    def _post(self, texts: Sequence[str]) -> list[list[float]]:
        payload = json.dumps({
            "model": self.model, "input": list(texts), "encoding_format": "float",
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/v1/embeddings", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = json.load(response)
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            raise RuntimeError(f"embedding request failed: {type(exc).__name__}") from exc
        rows = sorted(body.get("data") or [], key=lambda row: int(row.get("index", 0)))
        vectors = [row.get("embedding") for row in rows]
        if len(vectors) != len(texts) or any(not isinstance(vector, list) for vector in vectors):
            raise RuntimeError("embedding response shape mismatch")
        if any(len(vector) != BENCHMARK_IDENTITY.dimensions for vector in vectors):
            raise RuntimeError("embedding response dimension mismatch")
        if any(not math.isfinite(float(value)) for vector in vectors for value in vector):
            raise RuntimeError("embedding response contains non-finite values")
        return [[float(value) for value in vector] for vector in vectors]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._post(texts)

    def embed_query(self, query: str) -> list[float]:
        return self._post([serialize_bge_query(query)])[0]


def _percentile(values: Sequence[float], fraction: float) -> float:
    return sorted(values)[max(0, int(len(values) * fraction) - 1)] if values else 0.0


def _latency_summary(values: Sequence[float]) -> dict[str, float]:
    return {"p50": statistics.median(values) if values else 0.0, "p95": _percentile(values, 0.95)}


def _rank(expected: Sequence[str], rows: Sequence[dict[str, Any]]) -> int | None:
    ranks = [index for index, row in enumerate(rows, 1) if row["identity_key"] in expected]
    return min(ranks) if ranks else None


def _summary(
    results: list[dict[str, Any]],
    latencies: dict[str, list[float]],
    *,
    rejected_or_superseded: int,
    cross_run: int,
    secret_recall: int,
) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    for result in results:
        group = groups.setdefault(result["group"], {"count": 0, "hits": 0, "mrr_sum": 0.0})
        group["count"] += 1
        group["hits"] += int(result["hit"])
        group["mrr_sum"] += result["reciprocal_rank"]
    for group in groups.values():
        count = group.pop("count")
        group["recall_at_8"] = group.pop("hits") / count
        group["mrr_at_8"] = group.pop("mrr_sum") / count
    negatives = [result for result in results if result["group"] == "negative"]
    return {
        "query_count": len(results), "groups": groups,
        "overall_recall_at_8": sum(int(result["hit"]) for result in results) / len(results),
        "overall_mrr_at_8": sum(result["reciprocal_rank"] for result in results) / len(results),
        "negative_false_recall_rate": sum(int(result["returned_any"]) for result in negatives) / len(negatives),
        "negative_no_result_precision": sum(int(not result["returned_any"]) for result in negatives) / len(negatives),
        "context_chars_max": max((result["context_chars"] for result in results), default=0),
        "latency_ms": {name: _latency_summary(values) for name, values in latencies.items()},
        "rejected_or_superseded_leak_count": rejected_or_superseded,
        "cross_run_leak_count": cross_run,
        "secret_recall_count": secret_recall,
        "state_mutation_count": 0,
    }


def _eligible(store: SemanticGraphStore, run_a: str, query: dict[str, Any]) -> list[dict[str, Any]]:
    del query
    rows = store.list_nodes_for_run(run_a, statuses=["asserted", "accepted"], limit=5000)
    return [row for row in rows if float(row["confidence"]) >= 0.60]


def run_variant(
    store: SemanticGraphStore, fixture: dict[str, Any], run_a: str, *,
    dense: bool, client: HttpEmbeddingClient | None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    rejected_or_superseded = 0
    cross_run = 0
    secret_recall = 0
    latencies = {name: [] for name in (
        "query_embedding_ms", "lexical_ms", "dense_scan_ms", "rrf_ms", "end_to_end_ms",
    )}
    before = {row["node_id"]: (row["status"], row["authority"], row["confidence"])
              for row in store.list_nodes(limit=5000)}
    for query in fixture["queries"]:
        started = time.perf_counter()
        query_vector = None
        if dense:
            if client is None:
                raise RuntimeError("dense benchmark requires an embedding client")
            query_started = time.perf_counter()
            query_vector = client.embed_query(query["query"])
            latencies["query_embedding_ms"].append((time.perf_counter() - query_started) * 1000)

        lexical_started = time.perf_counter()
        lexical = search_and_rank(
            store, query["query"], top_k=LEXICAL_CANDIDATES, min_confidence=0.60,
            run_id=run_a if query.get("group") == "exact_identifier" else None,
        )
        latencies["lexical_ms"].append((time.perf_counter() - lexical_started) * 1000)
        rows = lexical[:TOP_K]

        if dense:
            eligible = _eligible(store, run_a, query)
            expected_hashes = {
                row["node_id"]: source_text_hash(serialize_embedding_node(row)) for row in eligible
            }
            dense_started = time.perf_counter()
            dense_rows = store.search_node_embeddings_exact(
                namespace=NAMESPACE, query_vector=query_vector, top_k=DENSE_CANDIDATES,
                node_ids=list(expected_hashes), expected_source_hashes=expected_hashes,
            )
            latencies["dense_scan_ms"].append((time.perf_counter() - dense_started) * 1000)
            rrf_started = time.perf_counter()
            eligible_ids = {row["node_id"] for row in eligible}
            lexical_safe = [row for row in lexical if row["node_id"] in eligible_ids]
            fused = reciprocal_rank_fusion(
                lexical_ids=[row["node_id"] for row in lexical_safe],
                dense_ids=[row["node_id"] for row in dense_rows],
                k=RRF_K,
                dense_similarities={row["node_id"]: row["similarity"] for row in dense_rows},
            )[:TOP_K]
            by_id = {row["node_id"]: row for row in eligible}
            by_id.update({row["node_id"]: row for row in lexical})
            rows = [by_id[item.node_id] for item in fused if item.node_id in by_id]
            latencies["rrf_ms"].append((time.perf_counter() - rrf_started) * 1000)

        rank = _rank(query["expected"], rows)
        rejected_or_superseded += sum(
            int(row.get("status") in {"rejected", "superseded"}) for row in rows
        )
        cross_run += sum(int(row.get("node_id") == "run-b-only") for row in rows)
        secret_recall += sum(int("opaque-secret" in json.dumps(row, ensure_ascii=False)) for row in rows)
        results.append({
            "group": query["group"], "expected": query["expected"],
            "returned": [row["identity_key"] for row in rows], "hit": rank is not None,
            "returned_any": bool(rows), "reciprocal_rank": 1.0 / rank if rank else 0.0,
            "context_chars": len(render_context(rows, 3500) or ""),
            "latency_ms": (time.perf_counter() - started) * 1000,
        })
        latencies["end_to_end_ms"].append(results[-1]["latency_ms"])

    after = {row["node_id"]: (row["status"], row["authority"], row["confidence"])
             for row in store.list_nodes(limit=5000)}
    summary = _summary(
        results,
        latencies,
        rejected_or_superseded=rejected_or_superseded,
        cross_run=cross_run,
        secret_recall=secret_recall,
    )
    summary["state_mutation_count"] = int(before != after)
    return summary


def run_benchmark(*, base_url: str, model: str, db_path: Path, live: bool, output: Path) -> dict[str, Any]:
    if not live:
        raise RuntimeError("live benchmark requires --live or SEMANTIC_GRAPH_LIVE_BENCHMARK=1")
    fixture = load_fixture()
    store, run_a, _ = make_store(db_path)
    client = HttpEmbeddingClient(base_url, model)
    node_rows = store.list_nodes(limit=5000)
    documents = [serialize_embedding_node(row) for row in node_rows]
    backfill_started = time.perf_counter()
    vectors = client.embed_documents(documents)
    for row, vector, document in zip(node_rows, vectors, documents):
        store.upsert_node_embedding(
            node_id=row["node_id"], identity=BENCHMARK_IDENTITY,
            vector=vector, source_text_hash=source_text_hash(document),
        )
    backfill_ms = (time.perf_counter() - backfill_started) * 1000
    lexical = run_variant(store, fixture, run_a, dense=False, client=None)
    hybrid = run_variant(store, fixture, run_a, dense=True, client=client)
    result = {
        "benchmark_schema_version": 1,
        "control_code_revision": CONTROL_CODE_REVISION,
        "benchmark_code_revision": benchmark_code_revision(),
        "control": CONTROL,
        "retrieval": {"top_k": TOP_K, "lexical_candidates": LEXICAL_CANDIDATES,
                      "dense_candidates": DENSE_CANDIDATES, "rrf_k": RRF_K},
        "backfill": {"document_count": len(documents), "duration_ms": backfill_ms},
        "variants": {"A_lexical": lexical, "B_hybrid_bge_m3": hybrid},
        "gates": {
            "japanese_to_english_recall_no_regression": hybrid["groups"].get("japanese_to_english", {}).get("recall_at_8") == 1.0,
            "leak_free": hybrid["rejected_or_superseded_leak_count"] == 0
            and hybrid["cross_run_leak_count"] == 0
            and hybrid["secret_recall_count"] == 0
            and hybrid["state_mutation_count"] == 0,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("BGE_M3_BASE_URL", "http://127.0.0.1:8084"))
    parser.add_argument("--model", default="nsfw-bge-m3-v4.gguf")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    live = args.live or os.environ.get("SEMANTIC_GRAPH_LIVE_BENCHMARK") == "1"
    result = run_benchmark(base_url=args.base_url, model=args.model, db_path=args.db, live=live, output=args.output)
    print(json.dumps({
        "control_code_revision": result["control_code_revision"],
        "benchmark_code_revision": result["benchmark_code_revision"],
        "variants": {key: value["overall_recall_at_8"] for key, value in result["variants"].items()},
        "output": "<benchmark-output>",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

__all__ = ["CONTROL_CODE_REVISION", "CONTROL", "HttpEmbeddingClient", "benchmark_code_revision", "make_store", "run_benchmark", "run_variant", "serialize_bge_query"]
