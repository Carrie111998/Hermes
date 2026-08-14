#!/usr/bin/env python3
"""Phase 0 retrieval gate — run the eval set against spine and report pass/fail.

Usage:
    python3 eval_run.py                     # run, print table
    python3 eval_run.py --save before.json  # also write raw results
    python3 eval_run.py --compare before.json   # diff against a saved run

A case passes when any of its `expect_any` phrases appears (case-insensitively)
in the content of the top-k hybrid recall results. This is deliberately a
coarse gate: it measures "did the right memory surface at all", not ranking
quality. Nothing in spine's retrieval path ships without a before/after run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # .../plugins/memory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(HERE))))  # hermes-agent root (for agent.* imports)

from spine.index import MemoryIndex  # noqa: E402
from spine import embedder  # noqa: E402

DB = os.path.expanduser("~/.hermes/memory.db")
EVAL = os.path.join(HERE, "eval_set.json")


def run(profile: str = "agent:main") -> Dict[str, Any]:
    spec = json.load(open(EVAL, encoding="utf-8"))
    k = spec.get("k", 6)
    cases = spec["cases"]

    idx = MemoryIndex(DB)
    idx.open()

    have_embedder = embedder.embedder_available()
    results: List[Dict[str, Any]] = []

    for case in cases:
        q = case["q"]
        t0 = time.perf_counter()
        qvec = embedder.embed_single(q) if have_embedder else None
        t_embed = time.perf_counter() - t0

        t0 = time.perf_counter()
        hits = idx.search_hybrid(q, qvec or None, profile=profile, k=k)
        t_search = time.perf_counter() - t0

        blob = "\n".join((h.get("content") or "") for h in hits).lower()
        matched = [p for p in case["expect_any"] if p.lower() in blob]

        results.append({
            "id": case["id"],
            "q": q,
            "passed": bool(matched),
            "matched": matched,
            "search_ms": round(t_search * 1000, 1),
            "embed_ms": round(t_embed * 1000, 1),
            "n_hits": len(hits),
            "n_wiki": sum(1 for h in hits if h.get("source") == "wiki"),
            "top": [
                {"src": h.get("source") or "obs",
                 "content": (h.get("content") or "")[:110]}
                for h in hits
            ],
        })

    idx.close()
    passed = sum(1 for r in results if r["passed"])
    return {
        "profile": profile,
        "embedder": have_embedder,
        "k": k,
        "passed": passed,
        "total": len(results),
        "median_search_ms": sorted(r["search_ms"] for r in results)[len(results) // 2],
        "cases": results,
    }


def show(rep: Dict[str, Any]) -> None:
    print(f"\nprofile={rep['profile']}  embedder={rep['embedder']}  k={rep['k']}")
    print(f"{'':2} {'case':24} {'result':6} {'ms':>7}  matched")
    print("-" * 78)
    for r in rep["cases"]:
        mark = "PASS" if r["passed"] else "FAIL"
        got = ", ".join(r["matched"])[:34] or "-"
        print(f"{'':2} {r['id']:24} {mark:6} {r['search_ms']:>7}  {got}")
    print("-" * 78)
    print(f"   {rep['passed']}/{rep['total']} passed   median search {rep['median_search_ms']} ms")
    wiki = sum(r["n_wiki"] for r in rep["cases"])
    print(f"   wiki chunks appearing in results: {wiki}\n")


def compare(new: Dict[str, Any], old_path: str) -> None:
    old = json.load(open(old_path, encoding="utf-8"))
    om = {c["id"]: c for c in old["cases"]}
    print(f"\n{'case':24} {'before':>8} {'after':>8}   {'ms before':>10} {'ms after':>9}")
    print("-" * 68)
    regressions = []
    for c in new["cases"]:
        o = om.get(c["id"])
        b = ("PASS" if o["passed"] else "FAIL") if o else "n/a"
        a = "PASS" if c["passed"] else "FAIL"
        flag = ""
        if o and o["passed"] and not c["passed"]:
            flag = "  <-- REGRESSION"
            regressions.append(c["id"])
        elif o and not o["passed"] and c["passed"]:
            flag = "  <-- fixed"
        ob = f"{o['search_ms']}" if o else "-"
        print(f"{c['id']:24} {b:>8} {a:>8}   {ob:>10} {c['search_ms']:>9}{flag}")
    print("-" * 68)
    print(f"{old['passed']}/{old['total']}  ->  {new['passed']}/{new['total']}")
    if regressions:
        print(f"REGRESSIONS: {', '.join(regressions)}")
    print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="agent:main")
    ap.add_argument("--save")
    ap.add_argument("--compare")
    a = ap.parse_args()

    rep = run(a.profile)
    show(rep)
    if a.save:
        json.dump(rep, open(a.save, "w", encoding="utf-8"), indent=2)
        print(f"saved -> {a.save}")
    if a.compare:
        compare(rep, a.compare)
