"""Isolated llama.cpp embedding capability probe (stdlib only)."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class ProbeConfig:
    base_url: str = "http://127.0.0.1:8082"
    model: str = "qwen3-vl-embedding-2b-q8_0"
    repo: str = "Etherll/Qwen3-VL-Embedding-2B-Q8_0-GGUF"
    expected_dimensions: int | None = 2048
    profile: str = "qwen3_vl_candidate"
    timeout_seconds: float = 10.0
    repeat_count: int = 20
    batch_sizes: tuple[int, ...] = (1, 2, 4, 8, 16, 32)
    soak_requests: int = 0
    soak_duration_seconds: float = 0.0
    gguf_path: str | None = None

    @property
    def normalized_base_url(self) -> str:
        return self.base_url.rstrip("/")


BASE_REQUIRED_CHECKS = (
    "health",
    "v1_models",
    "v1_embeddings",
    "one_vector_per_input",
    "all_finite",
    "all_nonzero",
    "unit_norm",
    "repeat_stability",
    "batch_stability",
    "semantic_ordering",
)


def required_check_names(*, soak_requests: int) -> tuple[str, ...]:
    if soak_requests < 0:
        raise ValueError("soak_requests must be non-negative")
    if soak_requests > 0:
        return BASE_REQUIRED_CHECKS + ("soak",)
    return BASE_REQUIRED_CHECKS


def determine_verdict(
    checks: Mapping[str, bool | None], *, soak_requests: int
) -> str:
    required = required_check_names(soak_requests=soak_requests)
    return (
        "pass_text_only"
        if all(checks.get(name) is True for name in required)
        else "fail_model_compatibility"
    )


@dataclass
class ProbeResult:
    schema_version: int = 1
    server: dict[str, Any] = field(default_factory=dict)
    model: dict[str, Any] = field(default_factory=dict)
    checks: dict[str, bool | None] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    verdict: str = "fail_model_compatibility"
    errors: list[str] = field(default_factory=list)
    observations: dict[str, Any] = field(default_factory=dict)
    soak: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "server": self.server,
            "model": self.model,
            "checks": self.checks,
            "metrics": self.metrics,
            "observations": self.observations,
            "soak": self.soak,
            "verdict": self.verdict,
            **({"errors": self.errors} if self.errors else {}),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


class ProbeTransportError(RuntimeError):
    pass


def _safe_url(base_url: str) -> str:
    value = base_url.rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise ValueError("base_url must use http or https")
    return value


def _request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: Mapping[str, Any] | None = None,
    timeout: float = 10.0,
) -> tuple[int, Any]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        _safe_url(base_url) + "/" + path.lstrip("/"),
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return int(response.status), json.loads(raw.decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        raise ProbeTransportError(f"request failed for {path}: {type(exc).__name__}") from exc


def build_payload(model: str, inputs: Sequence[str]) -> dict[str, Any]:
    return {"model": model, "encoding_format": "float", "input": list(inputs)}


def _scalar_vector(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, (int, float)) and not isinstance(item, bool) for item in value
    )


def check_shape(
    payload: Mapping[str, Any], expected_count: int, expected_dimensions: int | None
) -> tuple[bool, str, list[list[float]]]:
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != expected_count:
        return False, "embedding response count mismatch", []
    indices: list[int] = []
    vectors_by_index: dict[int, list[float]] = {}
    for item in data:
        if not isinstance(item, Mapping):
            return False, "embedding item is malformed", []
        index = item.get("index")
        embedding = item.get("embedding")
        if not isinstance(index, int) or index in indices:
            return False, "embedding indices are not unique integers", []
        if not _scalar_vector(embedding):
            return False, "embedding must be a flat vector, not a token matrix", []
        if not embedding:
            return False, "embedding vector is empty", []
        if expected_dimensions is not None and len(embedding) != expected_dimensions:
            return False, "embedding dimension mismatch", []
        indices.append(index)
        vectors_by_index[index] = [float(value) for value in embedding]
    if sorted(indices) != list(range(expected_count)):
        return False, "embedding indices do not cover 0..N-1", []
    vectors = [vectors_by_index[index] for index in range(expected_count)]
    dimensions = {len(vector) for vector in vectors}
    if len(dimensions) != 1:
        return False, "embedding dimensions drift within response", []
    return True, "", vectors


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("vectors must have the same non-zero dimension")
    left_norm = math.sqrt(math.fsum(value * value for value in left))
    right_norm = math.sqrt(math.fsum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        raise ValueError("zero vector")
    return math.fsum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def check_finite_norm(
    vectors: Sequence[Sequence[float]], *, tolerance: float = 0.001
) -> tuple[bool, str, dict[str, float]]:
    norms: list[float] = []
    for vector in vectors:
        try:
            values = [float(value) for value in vector]
        except (TypeError, ValueError):
            return False, "vector contains null or non-numeric value", {}
        if any(not math.isfinite(value) for value in values):
            return False, "vector contains NaN or Inf", {}
        norm = math.sqrt(math.fsum(value**2 for value in values))
        if norm == 0.0:
            return False, "vector is zero", {}
        norms.append(norm)
    if not norms or min(norms) < 1.0 - tolerance or max(norms) > 1.0 + tolerance:
        return False, "vector norm is outside tolerance", {}
    return True, "", {"norm_min": min(norms), "norm_max": max(norms)}


def check_repeat_stability(
    vectors: Sequence[Sequence[float]], *, threshold: float = 0.99999
) -> tuple[bool, str, dict[str, float]]:
    if not vectors:
        return False, "no repeat vectors", {}
    minimum = min(cosine(vectors[0], vector) for vector in vectors[1:]) if len(vectors) > 1 else 1.0
    return minimum >= threshold, "repeat cosine below threshold" if minimum < threshold else "", {"repeat_cosine_min": minimum}


def check_batch_stability(
    batch_vectors: Sequence[Sequence[float]],
    single_vectors: Sequence[Sequence[float]],
    *,
    threshold: float = 0.9999,
) -> tuple[bool, str, dict[str, float]]:
    if not batch_vectors or not single_vectors:
        return False, "missing batch vectors", {}
    minimum = min(cosine(single, batch) for single, batch in zip(single_vectors, batch_vectors))
    return minimum >= threshold, "batch cosine below threshold" if minimum < threshold else "", {"single_batch_cosine_min": minimum}


def check_semantic_ordering(
    query_vector: Sequence[float],
    document_vectors: Mapping[str, Sequence[float]],
    *,
    min_margin: float = 0.03,
) -> tuple[bool, str, dict[str, float]]:
    scores = {key: cosine(query_vector, value) for key, value in document_vectors.items()}
    required = all(key in scores for key in ("A", "B", "C"))
    ordered = required and scores["A"] > scores["B"] > scores["C"] and scores["A"] - scores["C"] >= min_margin
    metrics = {"positive_similarity": scores.get("A", 0.0), "negative_similarity": scores.get("C", 0.0), "semantic_margin": scores.get("A", 0.0) - scores.get("C", 0.0)}
    return ordered, "semantic ordering failed" if not ordered else "", metrics


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def make_report(
    config: ProbeConfig,
    *,
    checks: Mapping[str, bool | None],
    metrics: Mapping[str, float],
    actual_dimensions: int | None,
    errors: Sequence[str] = (),
    server: Mapping[str, Any] | None = None,
    observations: Mapping[str, Any] | None = None,
    soak: Mapping[str, Any] | None = None,
) -> ProbeResult:
    model = {
        "repo": config.repo,
        "alias": config.model,
        "gguf_sha256": None,
        "expected_dimensions": config.expected_dimensions,
        "actual_dimensions": actual_dimensions,
        "profile": config.profile,
    }
    if config.gguf_path:
        path = os.path.abspath(config.gguf_path)
        if os.path.isfile(path):
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            model["gguf_sha256"] = digest.hexdigest()
    verdict = determine_verdict(checks, soak_requests=config.soak_requests)
    soak_status = {
        "requested": config.soak_requests > 0,
        "request_count": config.soak_requests,
        "passed": None,
        **dict(soak or {}),
    }
    return ProbeResult(
        server={"base_url": config.normalized_base_url, **dict(server or {})},
        model=model,
        checks=dict(checks),
        metrics=dict(metrics),
        verdict=verdict,
        errors=list(errors),
        observations=dict(observations or {}),
        soak=soak_status,
    )


def run_probe_sync(config: ProbeConfig) -> ProbeResult:
    checks: dict[str, bool | None] = {"soak": None}
    metrics: dict[str, float] = {}
    errors: list[str] = []
    observations: dict[str, Any] = {}
    soak_result: dict[str, Any] = {}
    try:
        health_status, health_payload = _request_json(config.base_url, "/health", timeout=config.timeout_seconds)
        checks["health"] = health_status == 200 and isinstance(health_payload, Mapping)
        models_status, models_payload = _request_json(config.base_url, "/v1/models", timeout=config.timeout_seconds)
        checks["v1_models"] = models_status == 200 and isinstance(models_payload, Mapping)
        try:
            _request_json(config.base_url, "/props", timeout=config.timeout_seconds)
        except ProbeTransportError:
            pass
        inputs = [
            "The user prefers TypeScript for frontend development.",
            "The user uses Python for data analysis.",
            "The computer has an ASRock A320 motherboard.",
        ]
        started = time.perf_counter()
        status, payload = _request_json(config.base_url, "/v1/embeddings", method="POST", payload=build_payload(config.model, inputs), timeout=config.timeout_seconds)
        metrics["latency_ms_p50"] = (time.perf_counter() - started) * 1000.0
        if status != 200 or not isinstance(payload, Mapping):
            raise ValueError("embedding endpoint failed")
        ok, message, vectors = check_shape(payload, len(inputs), config.expected_dimensions)
        checks["v1_embeddings"] = status == 200
        checks["one_vector_per_input"] = ok
        if not ok:
            errors.append(message)
            return make_report(
                config,
                checks=checks,
                metrics=metrics,
                actual_dimensions=None,
                errors=errors,
                observations=observations,
                soak=soak_result,
            )
        actual_dimensions = len(vectors[0])
        finite_ok, message, norm_metrics = check_finite_norm(vectors)
        checks["all_finite"] = finite_ok
        checks["all_nonzero"] = finite_ok
        checks["unit_norm"] = finite_ok
        metrics.update(norm_metrics)
        repeat_status, repeat_payload = _request_json(config.base_url, "/v1/embeddings", method="POST", payload=build_payload(config.model, [inputs[0]] * config.repeat_count), timeout=config.timeout_seconds)
        repeat_ok, repeat_message, repeat_vectors = check_shape(repeat_payload, config.repeat_count, actual_dimensions)
        if repeat_status != 200 or not repeat_ok:
            checks["repeat_stability"] = False
            errors.append(repeat_message or "repeat request failed")
        else:
            repeat_check, repeat_message, repeat_metrics = check_repeat_stability(repeat_vectors)
            checks["repeat_stability"] = repeat_check
            metrics.update(repeat_metrics)
            if repeat_message:
                errors.append(repeat_message)
        batch_cosines: list[float] = []
        batch_latencies: list[float] = []
        batch_check = True
        batch_message = ""
        for batch_size in config.batch_sizes:
            if batch_size < 1:
                batch_check = False
                batch_message = "batch size must be positive"
                break
            batch_started = time.perf_counter()
            batch_status, batch_payload = _request_json(
                config.base_url,
                "/v1/embeddings",
                method="POST",
                payload=build_payload(config.model, [inputs[0]] * batch_size),
                timeout=config.timeout_seconds,
            )
            batch_latencies.append((time.perf_counter() - batch_started) * 1000.0)
            batch_ok, batch_message, batch_vectors = check_shape(
                batch_payload, batch_size, actual_dimensions
            )
            if batch_status != 200 or not batch_ok:
                batch_check = False
                break
            finite_ok, finite_message, _ = check_finite_norm(batch_vectors)
            if not finite_ok:
                batch_check = False
                batch_message = finite_message
                break
            batch_cosines.extend(
                cosine(repeat_vectors[0], vector) for vector in batch_vectors
            )
        checks["batch_stability"] = batch_check and bool(batch_cosines)
        if batch_cosines:
            metrics["single_batch_cosine_min"] = min(batch_cosines)
            metrics["latency_ms_p95"] = _percentile(batch_latencies, 0.95)
        if batch_message:
            errors.append(batch_message)

        checks["soak"] = None
        if config.soak_requests > 0:
            soak_check = True
            completed_requests = 0
            soak_started = time.perf_counter()
            for _ in range(config.soak_requests):
                soak_status, soak_payload = _request_json(
                    config.base_url,
                    "/v1/embeddings",
                    method="POST",
                    payload=build_payload(config.model, [inputs[0]]),
                    timeout=config.timeout_seconds,
                )
                soak_ok, soak_message, soak_vectors = check_shape(
                    soak_payload, 1, actual_dimensions
                )
                if soak_status != 200 or not soak_ok:
                    soak_check = False
                    errors.append(soak_message or "soak embedding request failed")
                    break
                finite_ok, finite_message, _ = check_finite_norm(soak_vectors)
                if not finite_ok:
                    soak_check = False
                    errors.append(finite_message or "soak vector validation failed")
                    break
                completed_requests += 1
            if config.soak_duration_seconds > 0:
                soak_check = soak_check and (
                    time.perf_counter() - soak_started >= config.soak_duration_seconds
                )
            checks["soak"] = soak_check
            soak_result.update(
                {
                    "passed": soak_check,
                    "completed_requests": completed_requests,
                    "failures": config.soak_requests - completed_requests,
                }
            )
        def ordering_for(query_text: str, *, profile: str) -> tuple[bool, str, dict[str, float]]:
            query_status, query_payload = _request_json(
                config.base_url,
                "/v1/embeddings",
                method="POST",
                payload=build_payload(config.model, [query_text]),
                timeout=config.timeout_seconds,
            )
            query_ok, query_message, query_vectors = check_shape(
                query_payload, 1, actual_dimensions
            )
            order_status, order_payload = _request_json(
                config.base_url,
                "/v1/embeddings",
                method="POST",
                payload=build_payload(config.model, inputs),
                timeout=config.timeout_seconds,
            )
            order_ok, order_message, order_vectors = check_shape(
                order_payload, 3, actual_dimensions
            )
            if query_status != 200 or order_status != 200 or not query_ok or not order_ok:
                return False, query_message or order_message or f"{profile} ordering request failed", {}
            return check_semantic_ordering(
                query_vectors[0],
                {"A": order_vectors[0], "B": order_vectors[1], "C": order_vectors[2]},
            )

        ordering_ok, ordering_message, ordering_metrics = ordering_for(
            "What frontend language does the user prefer?", profile="raw"
        )
        checks["semantic_ordering"] = ordering_ok
        observations["semantic_ordering"] = {
            "passed": ordering_ok,
            **ordering_metrics,
        }
        if ordering_message:
            observations["semantic_ordering"]["error"] = ordering_message

        japanese_ok, japanese_message, japanese_metrics = ordering_for(
            "フロントエンドでは何の言語を優先する？", profile="raw"
        )
        observations["japanese_cross_lingual"] = {
            "passed": japanese_ok,
            **japanese_metrics,
        }
        if japanese_message:
            observations["japanese_cross_lingual"]["error"] = japanese_message

        instruction_query = (
            "Instruct: Retrieve stable, provenance-backed memories relevant to the user's current query.\n"
            "Query:フロントエンドでは何の言語を優先する？"
        )
        profile_ok, profile_message, profile_metrics = ordering_for(
            instruction_query, profile="qwen3_text"
        )
        observations["instruction_profile_comparison"] = {
            "passed": profile_ok,
            "raw_margin": japanese_metrics.get("semantic_margin", 0.0),
            "instruction_margin": profile_metrics.get("semantic_margin", 0.0),
            "margin_delta": profile_metrics.get("semantic_margin", 0.0)
            - japanese_metrics.get("semantic_margin", 0.0),
        }
        if profile_message:
            observations["instruction_profile_comparison"]["error"] = profile_message

        return make_report(
            config,
            checks=checks,
            metrics=metrics,
            actual_dimensions=actual_dimensions,
            errors=errors,
            observations=observations,
            soak=soak_result,
        )
    except (ProbeTransportError, ValueError, TypeError) as exc:
        errors.append(str(exc))
        return make_report(
            config,
            checks=checks,
            metrics=metrics,
            actual_dimensions=None,
            errors=errors,
            observations=observations,
            soak=soak_result,
        )


def run_probe(config: ProbeConfig) -> ProbeResult:
    return run_probe_sync(config)


def _parse_batch_sizes(value: str) -> tuple[int, ...]:
    return tuple(int(item) for item in value.split(",") if item.strip())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8082")
    parser.add_argument("--model", default="qwen3-vl-embedding-2b-q8_0")
    parser.add_argument("--repo", default="Etherll/Qwen3-VL-Embedding-2B-Q8_0-GGUF")
    parser.add_argument("--expected-dimensions", type=int, default=2048)
    parser.add_argument("--profile", default="qwen3_vl_candidate")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--repeat-count", type=int, default=20)
    parser.add_argument("--batch-sizes", default="1,2,4,8,16,32")
    parser.add_argument("--soak-requests", type=int, default=0)
    parser.add_argument("--soak-duration", type=float, default=0.0)
    parser.add_argument("--gguf-path")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    result = run_probe_sync(ProbeConfig(base_url=args.base_url, model=args.model, repo=args.repo, expected_dimensions=args.expected_dimensions, profile=args.profile, timeout_seconds=args.timeout, repeat_count=args.repeat_count, batch_sizes=_parse_batch_sizes(args.batch_sizes), soak_requests=args.soak_requests, soak_duration_seconds=args.soak_duration, gguf_path=args.gguf_path))
    rendered = result.to_json()
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
    else:
        print(rendered)
    return 0 if result.verdict == "pass_text_only" else 1


if __name__ == "__main__":
    raise SystemExit(main())

__all__ = [
    "BASE_REQUIRED_CHECKS",
    "ProbeConfig",
    "ProbeResult",
    "build_payload",
    "check_shape",
    "check_finite_norm",
    "check_repeat_stability",
    "check_batch_stability",
    "check_semantic_ordering",
    "cosine",
    "determine_verdict",
    "make_report",
    "required_check_names",
    "run_probe",
    "run_probe_sync",
]
