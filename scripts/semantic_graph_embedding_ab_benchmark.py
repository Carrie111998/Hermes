"""Isolated lexical vs live BGE-M3 dense/RRF benchmark.

The benchmark owns a temporary SQLite database and does not call production
hybrid retrieval. Live HTTP execution is opt-in only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import statistics
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence

from plugins.memory.ebbinghaus.semantic_graph_bridge import project_retention
from plugins.semantic_graph.abstention import (
    DENSE_FLOOR,
    DENSE_MARGIN_FLOOR,
    DENSE_STRONG_FLOOR,
    RETENTION_FLOOR,
    RRF_MARGIN_FLOOR,
    SOURCE_COUNT_FLOOR,
    decide_abstention,
    extract_retrieval_features,
)
from plugins.semantic_graph.cognitive import (
    activate_cognitive_rerank,
    observe_cognitive_rerank,
)
from plugins.semantic_graph.embedding import EmbeddingModelIdentity, serialize_embedding_node, source_text_hash
from plugins.semantic_graph.fusion import reciprocal_rank_fusion
from plugins.semantic_graph.retrieval import render_context, search_and_rank
from plugins.semantic_graph.sanitize import normalize_text, sanitize_text
from plugins.semantic_graph.store import SemanticGraphStore

FIXTURE = Path(__file__).parents[1] / "tests" / "fixtures" / "semantic_graph_retrieval_benchmark.json"
COGNITIVE_FIXTURE = (
    Path(__file__).parents[1]
    / "tests"
    / "fixtures"
    / "semantic_graph_cognitive_benchmark.json"
)
CONTROL_CODE_REVISION = "0c89ef566343dbed810cedff81c9ac405febf0da"
CONTROL = {
    "repo": os.environ.get("BGE_M3_REPO", "KGESH/nsfw-bge-m3"),
    "family": "BGE-M3 fine-tune",
    "hf_revision": os.environ.get("BGE_M3_REVISION", "eaaf46c3b340d880d298ba7a03158fbe9b6e780b"),
    "gguf_sha256": os.environ.get(
        "BGE_M3_GGUF_SHA256",
        "d7579f0c22023eba0c4280f9ba52310710bba8677aaf868c7a625a0a7be50640",
    ),
    "dimensions": 1024,
    "serializer_profile": os.environ.get("BGE_M3_SERIALIZER_PROFILE", "bge_m3_control"),
}
BENCHMARK_IDENTITY = EmbeddingModelIdentity(
    provider=os.environ.get("BGE_M3_PROVIDER", "benchmark"),
    model=CONTROL["repo"],
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


def load_cognitive_fixture() -> dict[str, Any]:
    return json.loads(COGNITIVE_FIXTURE.read_text(encoding="utf-8"))


def _evaluation_split(seed: int, fixture_id: str) -> str:
    digest = hashlib.sha256(f"{seed}:{fixture_id}".encode("utf-8")).digest()
    return "holdout" if int.from_bytes(digest[:4], "big") % 5 == 0 else "development"


def build_cognitive_evaluation_fixture(
    base: dict[str, Any],
    spec: dict[str, Any],
) -> dict[str, Any]:
    seed = int(spec["seed"])
    queries: list[dict[str, Any]] = []

    def add(
        fixture_id: str,
        query: str,
        expected: list[str],
        group: str,
    ) -> None:
        queries.append({
            "fixture_id": fixture_id,
            "query": query,
            "expected": expected,
            "group": group,
            "split": _evaluation_split(seed, fixture_id),
        })

    for index, query in enumerate(base["queries"], start=1):
        add(
            f"base-{index:03d}",
            str(query["query"]),
            list(query["expected"]),
            str(query["group"]),
        )
    for topic_index, topic in enumerate(spec["negative_topics"], start=1):
        for template_index, template in enumerate(
            spec["negative_templates"], start=1
        ):
            add(
                f"negative-{topic_index:02d}-{template_index}",
                str(template).format(topic=topic),
                [],
                "negative_no_memory",
            )
    for index in range(1, 21):
        add(
            f"cue-disconnect-{index:02d}",
            str(spec["cue_disconnect_template"]).format(index=index),
            [],
            "cue_trigger_disconnect",
        )
    for index, query in enumerate(spec["tool_actions"], start=1):
        add(
            f"tool-grounding-{index:02d}",
            str(query),
            [],
            "tool_action_grounding",
        )
    correction_groups = (
        ("temporal_update", "temporal_update_templates"),
        ("contradiction", "contradiction_templates"),
        ("memory_poisoning_repair", "poisoning_repair_templates"),
    )
    for group, template_key in correction_groups:
        for correction_index, correction in enumerate(
            base["correction_nodes"], start=1
        ):
            token = str(correction["new_summary"]).rsplit(" ", 1)[-1].rstrip(".")
            for template_index, template in enumerate(
                spec[template_key], start=1
            ):
                add(
                    f"{group}-{correction_index:02d}-{template_index}",
                    str(template).format(token=token),
                    [str(correction["new_identity"])],
                    group,
                )
    return {
        **base,
        "cognitive_fixture_schema_version": int(spec["schema_version"]),
        "cognitive_fixture_seed": seed,
        "queries": queries,
    }


def select_evaluation_fixture(evaluation_split: str) -> dict[str, Any]:
    selected = str(evaluation_split or "baseline").strip().lower()
    if selected == "baseline":
        return load_fixture()
    complete = build_cognitive_evaluation_fixture(
        load_fixture(),
        load_cognitive_fixture(),
    )
    if selected == "all":
        return complete
    if selected not in {"development", "holdout"}:
        raise ValueError(
            "evaluation_split must be baseline, development, holdout, or all"
        )
    return {
        **complete,
        "queries": [
            query
            for query in complete["queries"]
            if query["split"] == selected
        ],
    }


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

    synced_at = time.time()
    cognitive_nodes = sorted(
        store.list_nodes_for_run(
            run_a,
            statuses=["asserted", "accepted"],
            limit=5000,
        ),
        key=lambda row: str(row["node_id"]),
    )
    for index, row in enumerate(cognitive_nodes, start=1):
        memory_id = 10_000 + index
        belief_id = f"benchmark-belief-{index}"
        access_state = (
            "latent"
            if index % 7 == 0
            else "reactivated"
            if index % 11 == 0
            else "accessible"
        )
        store.upsert_memory_node_link({
            "memory_id": memory_id,
            "node_id": row["node_id"],
            "belief_id": belief_id,
            "belief_version": 1,
            "relation": "represents",
        })
        store.upsert_memory_state_cache({
            "memory_id": memory_id,
            "belief_id": belief_id,
            "belief_version": 1,
            "access_state": access_state,
            "belief_status": "current",
            "memory_state": "active",
            "retention_at_sync": 0.55 + 0.10 * (index % 5),
            "stability_days": 30.0 + float(index % 7),
            "salience": float(row["salience"]),
            "valence": 0.0,
            "confidence": float(row["confidence"]),
            "protected": False,
            "source_updated_at": synced_at,
            "synced_at": synced_at,
        })
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


def _candidate_observations(
    fused: Sequence[Any], *, lexical_ids: Sequence[str], dense_ids: Sequence[str], top_k: int,
) -> list[dict[str, Any]]:
    """Serialize the complete pre-truncation fusion ranking for benchmark use."""
    lexical_ranks = {str(node_id): rank for rank, node_id in enumerate(lexical_ids, 1)}
    dense_ranks = {str(node_id): rank for rank, node_id in enumerate(dense_ids, 1)}
    observations: list[dict[str, Any]] = []
    for final_rank, candidate in enumerate(fused, 1):
        lexical_rank = lexical_ranks.get(candidate.node_id)
        dense_rank = dense_ranks.get(candidate.node_id)
        ranks = [rank for rank in (lexical_rank, dense_rank) if rank is not None]
        observations.append({
            "node_id": candidate.node_id,
            "lexical_rank": lexical_rank,
            "dense_rank": dense_rank,
            "dense_similarity": candidate.dense_similarity,
            "rrf_score": candidate.rrf_score,
            "source_count": candidate.source_count,
            "best_rank": min(ranks) if ranks else None,
            "final_rank": final_rank,
            "selected_into_top8": final_rank <= top_k,
        })
    return observations


def _cognitive_inputs(
    store: SemanticGraphStore,
) -> tuple[dict[str, list[dict[str, Any]]], dict[int, dict[str, Any]]]:
    links_by_node: dict[str, list[dict[str, Any]]] = {}
    for link in store.get_memory_node_links(limit=5000):
        links_by_node.setdefault(str(link["node_id"]), []).append(link)
    states_by_memory: dict[int, dict[str, Any]] = {}
    for cache in store.list_memory_state_cache(limit=5000):
        state = dict(cache)
        state["projected_retention"] = project_retention(
            cache,
            expected_belief_version=int(cache["belief_version"]),
        )
        states_by_memory[int(cache["memory_id"])] = state
    return links_by_node, states_by_memory


def _database_snapshot(store: SemanticGraphStore) -> tuple[str, ...]:
    uri = Path(store.db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return tuple(conn.iterdump())
    finally:
        conn.close()


def _query_observation(
    *, expected: Sequence[str], lexical_ids: Sequence[str], dense_ids: Sequence[str],
    dense_similarities: dict[str, float], fused_node_ids: Sequence[str], top_k: int,
) -> dict[str, Any]:
    """Aggregate score margins and channel agreement without changing retrieval."""
    del fused_node_ids, top_k
    fused = reciprocal_rank_fusion(
        lexical_ids=lexical_ids, dense_ids=dense_ids,
        k=RRF_K, dense_similarities=dense_similarities,
    )
    dense_scores = [dense_similarities.get(str(node_id)) for node_id in dense_ids]
    dense_scores = [float(score) for score in dense_scores if score is not None]
    rrf_scores = [candidate.rrf_score for candidate in fused]
    top1_dense = dense_scores[0] if dense_scores else None
    top2_dense = dense_scores[1] if len(dense_scores) > 1 else None
    top1_rrf = rrf_scores[0] if rrf_scores else None
    top2_rrf = rrf_scores[1] if len(rrf_scores) > 1 else None
    return {
        "top1_dense_similarity": top1_dense,
        "top2_dense_similarity": top2_dense,
        "dense_top_margin": top1_dense - top2_dense if top1_dense is not None and top2_dense is not None else None,
        "top1_rrf_score": top1_rrf,
        "top2_rrf_score": top2_rrf,
        "rrf_top_margin": top1_rrf - top2_rrf if top1_rrf is not None and top2_rrf is not None else None,
        "lexical_dense_top1_agreement": bool(lexical_ids and dense_ids and lexical_ids[0] == dense_ids[0]),
        "lexical_dense_expected_overlap": len(set(expected) & set(lexical_ids) & set(dense_ids)),
    }


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
        group = groups.setdefault(
            result["group"],
            {"count": 0, "hits": 0, "gated_hits": 0, "mrr_sum": 0.0},
        )
        group["count"] += 1
        group["hits"] += int(result["hit"])
        group["gated_hits"] += int(result["gated_hit"])
        group["mrr_sum"] += result["reciprocal_rank"]
    for group in groups.values():
        count = group.pop("count")
        group["recall_at_8"] = group.pop("hits") / count
        group["gated_recall_at_8"] = group.pop("gated_hits") / count
        group["mrr_at_8"] = group.pop("mrr_sum") / count
    negatives = [result for result in results if not result["expected"]]
    positives = [result for result in results if result["expected"]]
    true_abstain = sum(
        int(result["effective_no_result"]) for result in negatives
    )
    false_recall = len(negatives) - true_abstain
    false_abstain = sum(
        int(result["effective_no_result"]) for result in positives
    )
    true_allow = len(positives) - false_abstain
    predicted_no_result = true_abstain + false_abstain
    ungated_positive_recall = (
        sum(int(result["hit"]) for result in positives) / len(positives)
        if positives
        else 0.0
    )
    active_positive_recall = (
        sum(int(result["active_hit"]) for result in positives) / len(positives)
        if positives
        else 0.0
    )
    gated_positive_recall = (
        sum(int(result["gated_hit"]) for result in positives) / len(positives)
        if positives
        else 0.0
    )
    return {
        "query_count": len(results), "groups": groups,
        "overall_recall_at_8": sum(int(result["hit"]) for result in results) / len(results),
        "overall_mrr_at_8": sum(result["reciprocal_rank"] for result in results) / len(results),
        "negative_false_recall_rate": (
            false_recall / len(negatives) if negatives else 0.0
        ),
        "negative_no_result_precision": (
            true_abstain / predicted_no_result if predicted_no_result else 0.0
        ),
        "abstention": {
            "confusion_matrix": {
                "true_abstain": true_abstain,
                "false_recall": false_recall,
                "false_abstain": false_abstain,
                "true_allow": true_allow,
            },
            "ungated_positive_recall_at_8": ungated_positive_recall,
            "active_positive_recall_at_8": active_positive_recall,
            "gated_positive_recall_at_8": gated_positive_recall,
            "cognitive_active_degradation": (
                ungated_positive_recall - active_positive_recall
            ),
            "abstention_incremental_degradation": (
                active_positive_recall - gated_positive_recall
            ),
            "positive_recall_degradation": (
                ungated_positive_recall - gated_positive_recall
            ),
        },
        "context_chars_max": max((result["context_chars"] for result in results), default=0),
        "latency_ms": {name: _latency_summary(values) for name, values in latencies.items()},
        "rejected_or_superseded_leak_count": rejected_or_superseded,
        "cross_run_leak_count": cross_run,
        "secret_recall_count": secret_recall,
        "state_mutation_count": 0,
        "cognitive_shadow_observation_count": sum(
            len(result["candidates"]) for result in results
        ),
        "cognitive_rank_change_count": sum(
            int(candidate["cognitive_shadow"]["rank_changed"])
            for result in results
            for candidate in result["candidates"]
        ),
        "cognitive_would_filter_count": sum(
            int(candidate["cognitive_shadow"]["would_filter"])
            for result in results
            for candidate in result["candidates"]
        ),
        "query_results": results,
    }


def assess_abstention_acceptance(summary: dict[str, Any]) -> dict[str, Any]:
    """Evaluate only the fixed Phase 5 holdout acceptance contract."""
    correction_groups = (
        "correction_history",
        "temporal_update",
        "contradiction",
        "memory_poisoning_repair",
    )
    groups = summary.get("groups") or {}
    correction_history_no_regression = all(
        group in groups
        and float(groups[group].get("gated_recall_at_8", -1.0))
        >= float(groups[group].get("recall_at_8", 0.0))
        for group in correction_groups
    )
    checks = {
        "positive_recall_degradation": float(
            summary.get("abstention", {}).get(
                "positive_recall_degradation", float("inf")
            )
        )
        <= 0.02,
        "correction_history_no_regression": correction_history_no_regression,
        "rejected_or_superseded_leak": int(
            summary.get("rejected_or_superseded_leak_count", -1)
        )
        == 0,
        "cross_run_leak": int(summary.get("cross_run_leak_count", -1)) == 0,
        "secret_recall": int(summary.get("secret_recall_count", -1)) == 0,
        "state_mutation": int(summary.get("state_mutation_count", -1)) == 0,
        "negative_false_recall_rate": float(
            summary.get("negative_false_recall_rate", float("inf"))
        )
        <= 0.20,
        "negative_no_result_precision": float(
            summary.get("negative_no_result_precision", float("-inf"))
        )
        >= 0.80,
    }
    return {"checks": checks, "pass": all(checks.values())}


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
    before = _database_snapshot(store)
    links_by_node, states_by_memory = _cognitive_inputs(store)
    for query_index, query in enumerate(fixture["queries"], start=1):
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
        dense_rows: list[dict[str, Any]] = []
        lexical_safe = lexical
        eligible: list[dict[str, Any]] = []
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
            eligible_ids = {row["node_id"] for row in eligible}
            lexical_safe = [row for row in lexical if row["node_id"] in eligible_ids]

        rrf_started = time.perf_counter()
        dense_similarities = {row["node_id"]: row["similarity"] for row in dense_rows}
        fused = reciprocal_rank_fusion(
            lexical_ids=[row["node_id"] for row in lexical_safe],
            dense_ids=[row["node_id"] for row in dense_rows],
            k=RRF_K,
            dense_similarities=dense_similarities,
        )
        base_candidates = _candidate_observations(
            fused,
            lexical_ids=[row["node_id"] for row in lexical_safe],
            dense_ids=[row["node_id"] for row in dense_rows],
            top_k=TOP_K,
        )
        candidates = observe_cognitive_rerank(
            base_candidates,
            links_by_node=links_by_node,
            states_by_memory=states_by_memory,
            query_mode="normal",
        )
        active_candidates = activate_cognitive_rerank(
            observe_cognitive_rerank(
                base_candidates[:TOP_K],
                links_by_node=links_by_node,
                states_by_memory=states_by_memory,
                query_mode="normal",
            )
        )
        observation = _query_observation(
            expected=query["expected"],
            lexical_ids=[row["node_id"] for row in lexical_safe],
            dense_ids=[row["node_id"] for row in dense_rows],
            dense_similarities=dense_similarities,
            fused_node_ids=[item.node_id for item in fused],
            top_k=TOP_K,
        )
        by_id = {row["node_id"]: row for row in (eligible or lexical)}
        by_id.update({row["node_id"]: row for row in lexical})
        rows = [by_id[item.node_id] for item in fused[:TOP_K] if item.node_id in by_id]
        active_rows = [
            by_id[str(candidate["node_id"])]
            for candidate in active_candidates
            if str(candidate["node_id"]) in by_id
        ]
        if dense:
            latencies["rrf_ms"].append((time.perf_counter() - rrf_started) * 1000)

        rank = _rank(query["expected"], rows)
        active_rank = _rank(query["expected"], active_rows)
        features = extract_retrieval_features(
            active_candidates,
            query_length=len(str(query["query"])),
        )
        abstention = decide_abstention(features)
        production_rows = [] if abstention["abstain"] else active_rows
        effective_no_result = not production_rows
        rejected_or_superseded += sum(
            int(row.get("status") in {"rejected", "superseded"})
            for row in production_rows
        )
        cross_run += sum(
            int(row.get("node_id") == "run-b-only")
            for row in production_rows
        )
        secret_recall += sum(
            int("opaque-secret" in json.dumps(row, ensure_ascii=False))
            for row in production_rows
        )
        results.append({
            "fixture_id": str(query.get("fixture_id") or f"q-{query_index:03d}"),
            "split": str(query.get("split") or "baseline"),
            "group": query["group"], "expected": query["expected"],
            "returned": [row["identity_key"] for row in rows], "hit": rank is not None,
            "active_returned": [row["identity_key"] for row in active_rows],
            "active_hit": active_rank is not None,
            "active_reciprocal_rank": 1.0 / active_rank if active_rank else 0.0,
            "active_returned_any": bool(active_rows),
            "gated_hit": active_rank is not None and not effective_no_result,
            "returned_any": bool(rows), "reciprocal_rank": 1.0 / rank if rank else 0.0,
            "effective_no_result": effective_no_result,
            "context_chars": len(render_context(production_rows, 3500) or ""),
            "latency_ms": (time.perf_counter() - started) * 1000,
            "candidates": candidates,
            "observation": observation,
            "features": features,
            "abstention": abstention,
        })
        latencies["end_to_end_ms"].append(results[-1]["latency_ms"])

    after = _database_snapshot(store)
    summary = _summary(
        results,
        latencies,
        rejected_or_superseded=rejected_or_superseded,
        cross_run=cross_run,
        secret_recall=secret_recall,
    )
    summary["state_mutation_count"] = int(before != after)
    return summary


def run_benchmark(
    *,
    base_url: str,
    model: str,
    db_path: Path,
    live: bool,
    output: Path,
    evaluation_split: str = "baseline",
) -> dict[str, Any]:
    if not live:
        raise RuntimeError("live benchmark requires --live or SEMANTIC_GRAPH_LIVE_BENCHMARK=1")
    fixture = select_evaluation_fixture(evaluation_split)
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
    acceptance = assess_abstention_acceptance(hybrid)
    result = {
        "benchmark_schema_version": 3,
        "evaluation_split": evaluation_split,
        "control_code_revision": CONTROL_CODE_REVISION,
        "benchmark_code_revision": benchmark_code_revision(),
        "control": CONTROL,
        "retrieval": {"top_k": TOP_K, "lexical_candidates": LEXICAL_CANDIDATES,
                      "dense_candidates": DENSE_CANDIDATES, "rrf_k": RRF_K},
        "backfill": {"document_count": len(documents), "duration_ms": backfill_ms},
        "variants": {"A_lexical": lexical, "B_hybrid_bge_m3": hybrid},
        "cognitive_shadow": {
            "mode": "shadow",
            "formula": "fixed-v1",
            "query_mode": "normal",
            "ebbinghaus_db_accessed": False,
        },
        "abstention_gate": {
            "dense_floor": DENSE_FLOOR,
            "dense_strong_floor": DENSE_STRONG_FLOOR,
            "dense_margin_floor": DENSE_MARGIN_FLOOR,
            "rrf_margin_floor": RRF_MARGIN_FLOOR,
            "source_count_floor": SOURCE_COUNT_FLOOR,
            "retention_floor": RETENTION_FLOOR,
        },
        "phase5_acceptance": {
            "evaluation_split": evaluation_split,
            **acceptance,
        },
        "gates": {
            "japanese_to_english_recall_no_regression": hybrid["groups"].get("japanese_to_english", {}).get("recall_at_8") == 1.0,
            "leak_free": hybrid["rejected_or_superseded_leak_count"] == 0
            and hybrid["cross_run_leak_count"] == 0
            and hybrid["secret_recall_count"] == 0
            and hybrid["state_mutation_count"] == 0,
            "shadow_read_only": lexical["state_mutation_count"] == 0
            and hybrid["state_mutation_count"] == 0,
            "phase5_acceptance": acceptance["pass"],
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("BGE_M3_BASE_URL", "http://127.0.0.1:8084"))
    parser.add_argument("--model", default=os.environ.get("BGE_M3_MODEL", "nsfw-bge-m3-v4.gguf"))
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--evaluation-split",
        choices=["baseline", "development", "holdout", "all"],
        default="baseline",
    )
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    live = args.live or os.environ.get("SEMANTIC_GRAPH_LIVE_BENCHMARK") == "1"
    result = run_benchmark(
        base_url=args.base_url,
        model=args.model,
        db_path=args.db,
        live=live,
        output=args.output,
        evaluation_split=args.evaluation_split,
    )
    print(json.dumps({
        "control_code_revision": result["control_code_revision"],
        "benchmark_code_revision": result["benchmark_code_revision"],
        "variants": {key: value["overall_recall_at_8"] for key, value in result["variants"].items()},
        "output": "<benchmark-output>",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

__all__ = [
    "CONTROL_CODE_REVISION", "CONTROL", "HttpEmbeddingClient", "benchmark_code_revision",
    "assess_abstention_acceptance",
    "build_cognitive_evaluation_fixture", "load_cognitive_fixture",
    "select_evaluation_fixture",
    "make_store", "run_benchmark", "run_variant", "serialize_bge_query",
    "_candidate_observations", "_query_observation",
]
