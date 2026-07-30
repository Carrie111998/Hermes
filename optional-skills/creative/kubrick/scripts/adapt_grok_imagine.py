#!/usr/bin/env python3
"""Translate a neutral Kubrick adapter packet into Grok Imagine prompt packets."""
from __future__ import annotations
import argparse, json
from pathlib import Path
try:
    import yaml
except ImportError:
    raise SystemExit("pyyaml required")


def load(path: str) -> dict:
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.suffix == ".json" else yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--packet", required=True)
    p.add_argument("--output")
    args = p.parse_args()
    packet = load(args.packet)
    errors = list(packet.get("validation", {}).get("errors", []))
    if packet.get("validation", {}).get("status") != "VALID":
        errors.append("neutral adapter packet is not VALID")
    if packet.get("provider") not in {"generic", "grok-imagine"}:
        errors.append("packet provider is incompatible with Grok Imagine adapter")
    frames = []
    for frame in packet.get("frames", []):
        prompt_parts = [frame.get("prompt", "")]
        prompt_parts.extend(frame.get("continuity_from_previous", []))
        prompt_parts.extend(frame.get("state_constraints", []))
        prompt_parts.extend(packet.get("shared_constraints", {}).get("negative_constraints", []))
        frames.append({
            "frame_id": frame.get("frame_id"),
            "prompt": "; ".join(str(x).strip() for x in prompt_parts if str(x).strip()),
            "continuity_lock": frame.get("continuity_from_previous", []),
            "negative_constraints": packet.get("shared_constraints", {}).get("negative_constraints", []),
            "generation_parameters": {
                "aspect_ratio": frame.get("provider_parameters", {}).get("aspect_ratio", "16:9"),
                "style_strength": frame.get("provider_parameters", {}).get("style_strength", "restrained"),
                "variation_policy": "preserve identity; vary only declared frame state",
            },
        })
    result = {
        "adapter": "grok-imagine-v1",
        "source_adapter_id": packet.get("adapter_id"),
        "source_graph_id": packet.get("source_graph_id"),
        "frames": frames,
        "private_state_policy": packet.get("private_state_policy"),
        "validation": {"status": "VALID" if not errors and frames else "INVALID", "errors": errors or ([] if frames else ["no frames available"])}
    }
    text = yaml.safe_dump(result, sort_keys=False)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        print(text)
    raise SystemExit(0 if result["validation"]["status"] == "VALID" else 1)


if __name__ == "__main__":
    main()
