from __future__ import annotations

import asyncio
import json

import pytest

from gateway.run import GatewayRunner


@pytest.mark.asyncio
async def test_vision_preanalysis_fans_out_with_order_preserved(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._vision_preanalysis_max_concurrency = lambda: 4  # type: ignore[method-assign]

    active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def fake_vision_analyze_tool(*, image_url: str, user_prompt: str) -> str:
        nonlocal active, max_active
        async with lock:
            active += 1
            max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        async with lock:
            active -= 1
        return json.dumps({"success": True, "analysis": f"analysis for {image_url}"})

    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", fake_vision_analyze_tool)

    paths = [f"/tmp/image-{index}.jpg" for index in range(7)]
    enriched = await runner._enrich_message_with_vision("caption", paths)

    assert max_active == 4
    assert enriched.endswith("caption")
    positions = [enriched.index(f"analysis for {path}") for path in paths]
    assert positions == sorted(positions)
