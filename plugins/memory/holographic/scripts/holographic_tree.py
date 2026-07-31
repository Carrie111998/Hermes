#!/usr/bin/env python3
"""
Interactive Holographic Memory Tree Viewer
Navigate facts by category -> entity -> individual facts.
"""

import sys
import json
from pathlib import Path
from datetime import datetime, timedelta

# Resolve the holographic provider module from the plugin loader so this
# script works from any checkout or user-installed plugin directory —
# no hardcoded contributor-home path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent.parent))

from plugins.memory.holographic import HolographicMemoryProvider, _load_plugin_config
from rich.console import Console
from rich.tree import Tree


# ── Tree building functions (exported for testing) ──────────────────────────

def build_tree(facts):
    """Build a nested tree structure: Category -> Entity -> Facts."""
    tree_data = {}

    for fact in facts:
        category = fact.get("category", "uncategorized")
        tags = fact.get("tags", "").split(",") if fact.get("tags") else []

        # Extract entity from tags (look for entity: prefix)
        entity = None
        for tag in tags:
            tag = tag.strip()
            if tag.startswith("entity:"):
                entity = tag[7:]
                break

        # If no entity tag, try to infer from content or use "general"
        if not entity:
            entity = "general"

        # Build nested structure
        if category not in tree_data:
            tree_data[category] = {}
        if entity not in tree_data[category]:
            tree_data[category][entity] = []
        tree_data[category][entity].append(fact)

    return tree_data


def get_time_bucket(created_at_str, now=None):
    """Categorize facts by time."""
    try:
        dt = datetime.strptime(created_at_str, "%Y-%m-%d %H:%M:%S")
    except:
        return "Unknown"

    if now is None:
        now = datetime.now()
    diff = now - dt

    if diff.days == 0:
        return "Today"
    elif diff.days <= 7:
        return "This Week"
    elif diff.days <= 30:
        return "This Month"
    else:
        return "Older"


# ── Interactive tree viewer ────────────────────────────────────────────────

console = Console()


def load_facts():
    """Load all facts from the holographic memory store."""
    config = _load_plugin_config()
    provider = HolographicMemoryProvider(config=config)
    provider.initialize("tree-viewer-session")
    result = provider._handle_fact_store({
        "action": "list",
        "limit": 1000,
        "min_trust": 0.0
    })
    facts = json.loads(result)["facts"]
    return facts


def build_rich_tree(tree_data, selected_path=None):
    """Build a rich.tree.Tree from the nested data."""
    root = Tree("🧠 [bold cyan]Holographic Memory[/bold cyan]")
    
    # Sort categories
    for category in sorted(tree_data.keys()):
        cat_node = root.add(f"[bold yellow]📁 {category}[/bold yellow]")
        
        # Sort entities
        for entity in sorted(tree_data[category].keys()):
            facts = tree_data[category][entity]
            entity_label = f"[green]🏷️  {entity}[/green] ({len(facts)} facts)"
            entity_node = cat_node.add(entity_label)
            
            # Sort facts by date (newest first)
            facts_sorted = sorted(facts, key=lambda f: f.get("created_at", ""), reverse=True)
            
            # Group by time bucket
            time_buckets = {}
            for fact in facts_sorted:
                bucket = get_time_bucket(fact.get("created_at", ""))
                if bucket not in time_buckets:
                    time_buckets[bucket] = []
                time_buckets[bucket].append(fact)
            
            for bucket_name in ["Today", "This Week", "This Month", "Older", "Unknown"]:
                if bucket_name in time_buckets:
                    bucket_facts = time_buckets[bucket_name]
                    bucket_node = entity_node.add(f"[dim]📅 {bucket_name}[/dim] ({len(bucket_facts)})")
                    
                    for fact in bucket_facts:
                        fid = fact.get("fact_id", "?")
                        trust = fact.get("trust_score", 0.0)
                        trust_color = "green" if trust >= 0.7 else "yellow" if trust >= 0.4 else "red"
                        content_preview = fact.get("content", "")[:80] + ("..." if len(fact.get("content", "")) > 80 else "")
                        tags_str = fact.get("tags", "")
                        fact_label = f"[dim]#{fid}[/dim] [{trust_color}]{trust:.2f}[/{trust_color}] {content_preview}"
                        if tags_str:
                            fact_label += f" [dim]({tags_str[:40]})[/dim]"
                        bucket_node.add(fact_label)
    
    return root


# ── Interactive tree viewer ────────────────────────────────────────────────


def render_tree():
    """Render the full memory tree once, then exit. No interactive input."""
    facts = load_facts()
    tree_data = build_tree(facts)

    console.print("[bold cyan]Holographic Memory Tree Viewer[/bold cyan]")
    console.print()
    tree = build_rich_tree(tree_data)
    console.print(tree)


def main():
    try:
        render_tree()
    except KeyboardInterrupt:
        console.print("\n[yellow]Exiting...[/yellow]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()