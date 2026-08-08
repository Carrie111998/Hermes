#!/usr/bin/env python3
"""
Dream Synthesizer: Associate low-salience memories (0.3-0.6) to produce
a candidate insight stored as a low-salience memory with tag 'dream-candidate'.
"""
import json
import os
import sys

REPO = os.environ.get("HERMES_REPO", r"C:\Users\downl\Documents\New project\hermes-agent")
sys.path.insert(0, REPO)

from plugins.memory.ebbinghaus import EbbinghausMemoryProvider
from hermes_constants import get_hermes_home


def main():
    p = EbbinghausMemoryProvider()
    p.initialize("dream-synthesizer-cron", hermes_home=str(get_hermes_home()))

    # 1. Get current stats
    pre_stats = json.loads(p.handle_tool_call("ebbinghaus_memory", {"action": "stats"}))
    print(f"[pre] total={pre_stats.get('count', 0)}, active={pre_stats.get('active_count', 0)}, avg_salience={pre_stats.get('avg_salience', 0):.3f}")

    # 2. List memories in salience range 0.3-0.6
    # We need to query the store directly for this
    # Let's use list action with a high limit and filter
    list_result = json.loads(p.handle_tool_call("ebbinghaus_memory", {"action": "list", "limit": 500, "include_archived": False}))
    memories = list_result.get("memories", [])

    low_salience = [m for m in memories if 0.3 <= float(m.get("salience", 0)) <= 0.6]
    print(f"[scan] Found {len(low_salience)} memories with salience 0.3-0.6")

    if not low_salience:
        print("[result] No low-salience memories to synthesize")
        p.shutdown()
        return

    # 3. Mark them as dream candidates
    candidate_ids = [int(m["memory_id"]) for m in low_salience]
    for mid in candidate_ids:
        # Use the store directly to update dream_candidate flag
        p._store._conn.execute(
            "UPDATE memories SET dream_candidate = 1 WHERE memory_id = ?",
            (mid,)
        )
    p._store._conn.commit()
    print(f"[mark] Marked {len(candidate_ids)} memories as dream candidates")

    # 4. Run dream preview to get clusters
    preview = json.loads(p.handle_tool_call("ebbinghaus_memory", {"action": "dream", "mode": "preview"}))
    print(f"[preview] Clusters found: {len(preview.get('clusters', []))}")

    clusters = preview.get("clusters", [])
    if not clusters:
        print("[result] No clusters formed from low-salience memories")
        p.shutdown()
        return

    # 5. Synthesize insight from clusters
    # We'll create a summary from the themes of the clusters
    all_themes = []
    all_source_ids = []
    for cluster in clusters:
        all_themes.extend(cluster.get("themes", []))
        all_source_ids.extend(cluster.get("source_memory_ids", []))

    # Deduplicate themes
    unique_themes = list(dict.fromkeys(all_themes))[:8]

    # Create a synthesized insight
    theme_str = "、".join(unique_themes) if unique_themes else "多様な話題"
    insight_content = f"低顕著度記憶(0.3-0.6)のクラスタリングから得られた示唆: {theme_str} に関する断片的記憶が潜在的に関連している可能性"

    # 6. Store as a low-salience memory with tag 'dream-candidate'
    apply_result = json.loads(p.handle_tool_call("ebbinghaus_memory", {
        "action": "dream",
        "mode": "apply",
        "dreams": [{
            "cluster_id": clusters[0]["cluster_id"],
            "summary": insight_content,
            "tags": ["dream-candidate", "semantic", "synthesis"],
            "salience": 0.35,  # Low salience as requested
            "valence": 0.1,
            "source_memory_ids": all_source_ids[:20]  # Limit to first 20
        }]
    }))
    print(f"[apply] Result: {apply_result}")

    # 7. Final stats
    post_stats = json.loads(p.handle_tool_call("ebbinghaus_memory", {"action": "stats"}))
    print(f"[post] total={post_stats.get('count', 0)}, active={post_stats.get('active_count', 0)}, avg_salience={post_stats.get('avg_salience', 0):.3f}")

    p.shutdown()
    print("[done] Dream synthesis complete")


if __name__ == "__main__":
    main()
