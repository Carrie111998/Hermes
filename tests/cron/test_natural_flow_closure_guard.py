from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT_PATH = Path("/Users/yaphie/.hermes/scripts/natural_flow_closure_guard.py")


@pytest.fixture(scope="module")
def closure_guard():
    spec = importlib.util.spec_from_file_location("natural_flow_closure_guard", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_publish_evidence_blob_ignores_body_field_names(closure_guard):
    task = {
        "title": "Takeflow 发布闭环",
        "body": "需要确认 backend_task_id / backend_detail_id 是否存在，但这里只是要求，不是结果。",
        "result": "",
    }

    blob = closure_guard.publish_evidence_blob(task, [], [])

    assert "backend_task_id" not in blob
    assert "backend_detail_id" not in blob
    assert closure_guard.has_publish_done_evidence(blob) is False


def test_publish_done_evidence_requires_real_values(closure_guard):
    blob = "\n".join(
        [
            "已发布成功",
            "publish_ledger.json 已记录",
            'backend_task_id: "12345"',
            'backend_detail_id: "67890"',
        ]
    )

    assert closure_guard.has_publish_done_evidence(blob) is True


def test_takeflow_publish_json_gate_rejects_todo_state(tmp_path, monkeypatch, closure_guard):
    monkeypatch.setattr(closure_guard, "TAKEFLOW_TASKS_DIR", tmp_path)
    task_name = "chinese_seal_incense_tiktok_task.json"
    task_path = tmp_path / task_name
    task_path.write_text(
        json.dumps(
            {
                "strategy": {"workflow": "tiktok-native-publish", "platform": "tiktok"},
                "state": "todo",
                "artifacts": {
                    "final_verification": {
                        "surface": "backend_anchor_api",
                        "endpoint": "/task/main-task/createShortDramaTask",
                        "anchor_link_used": "https://www.tiktok.com/t/example",
                        "material_path": "/tmp/video.mp4",
                        "release_scheduled_time_match": True,
                        "backend_task_id": "abc123",
                        "backend_detail_id": "def456",
                    }
                },
                "takeflow": {"seat_label": "22号坐席"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    gate = closure_guard.takeflow_publish_json_gate(f"tiktok {task_name}")

    assert gate["applies"] is True
    assert gate["ok"] is False
    assert any("state=todo" in failure for failure in gate["failures"])


def test_repair_owner_keeps_aivideo_publish_task_with_publish_ops(closure_guard):
    task = {
        "assignee": "nf-publish-ops",
        "title": "Takeflow发布｜AIvideo TikTok 坐席24 普通发布 每日2条",
    }

    assert closure_guard.repair_owner(task) == (
        "nf-publish-ops",
        ["natural-flow-growth-operations"],
    )


def test_repair_owner_uses_current_business_delivery_skill_for_appdev(closure_guard):
    task = {
        "assignee": "appdev-worker-1",
        "title": "App implementation repair",
    }

    assert closure_guard.repair_owner(task) == (
        "bizline2",
        ["business-delivery-operations"],
    )


def test_extract_direction_recognizes_aivideo_display_name(closure_guard):
    assert closure_guard.extract_direction("Takeflow发布｜AIvideo TikTok 坐席24 普通发布") == "ai_video"
