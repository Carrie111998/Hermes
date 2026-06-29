from __future__ import annotations

import json


def _load_tool(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import importlib
    import agent.ultra_media_store as store
    import tools.ultra_media_job_tool as tool

    importlib.reload(store)
    return importlib.reload(tool)


def test_create_image_job_persists_asset_and_events(monkeypatch, tmp_path):
    tool = _load_tool(monkeypatch, tmp_path)

    def fake_dispatch(name, args, **kwargs):
        assert name == "image_generate"
        assert args["prompt"] == "studio product photo"
        assert kwargs["session_id"] == "sess-1"
        return json.dumps(
            {
                "success": True,
                "image": "https://cdn.example/out.png",
                "provider": "atlas",
                "model": "nano-banana-pro",
                "prompt": args["prompt"],
                "modality": "text",
                "aspect_ratio": "landscape",
            }
        )

    monkeypatch.setattr(tool.registry, "dispatch", fake_dispatch)

    result = json.loads(
        tool._handle_ultra_media_job_create(
            {"media_type": "image", "prompt": "studio product photo"},
            task_id="run-1",
            session_id="sess-1",
        )
    )

    assert result["success"] is True
    assert result["status"] == "succeeded"
    assert result["asset"]["uri"] == "https://cdn.example/out.png"
    assert result["asset"]["media_type"] == "image"
    assert result["job"]["session_id"] == "sess-1"
    assert result["job"]["run_id"] == "run-1"
    assert result["job"]["provider"] == "atlas"
    assert result["job"]["model"] == "nano-banana-pro"
    assert [event["event_type"] for event in result["events"]] == [
        "media_job.created",
        "media_job.updated",
        "media_job.updated",
        "asset.ready",
    ]


def test_create_video_job_records_provider_failure(monkeypatch, tmp_path):
    tool = _load_tool(monkeypatch, tmp_path)

    def fake_dispatch(name, args, **kwargs):
        assert name == "video_generate"
        return json.dumps(
            {
                "success": False,
                "video": None,
                "error": "ATLAS_API_KEY not set",
                "error_type": "auth_required",
                "provider": "atlas",
                "model": "wan-2.6-flash",
            }
        )

    monkeypatch.setattr(tool.registry, "dispatch", fake_dispatch)

    result = json.loads(
        tool._handle_ultra_media_job_create(
            {"media_type": "video", "prompt": "a launch teaser"},
            task_id="run-2",
            session_id="sess-2",
        )
    )

    assert result["success"] is False
    assert result["status"] == "failed"
    assert result["job"]["error"]["error_type"] == "auth_required"
    assert result["job"]["provider"] == "atlas"
    assert result["job"]["model"] == "wan-2.6-flash"
    assert [event["event_type"] for event in result["events"]] == [
        "media_job.created",
        "media_job.updated",
        "media_job.failed",
    ]


def test_status_returns_persisted_job(monkeypatch, tmp_path):
    tool = _load_tool(monkeypatch, tmp_path)

    monkeypatch.setattr(
        tool.registry,
        "dispatch",
        lambda *_args, **_kwargs: json.dumps(
            {
                "success": True,
                "video": "https://cdn.example/out.mp4",
                "provider": "atlas",
                "model": "wan-2.6-flash",
                "prediction_id": "pred-1",
            }
        ),
    )

    created = json.loads(
        tool._handle_ultra_media_job_create(
            {
                "media_type": "video",
                "prompt": "animate",
                "duration": 5,
                "auto_finalize": False,
            },
            session_id="sess-3",
        )
    )
    status = json.loads(tool._handle_ultra_media_job_status({"job_id": created["job_id"]}))

    assert status["success"] is True
    assert status["job"]["status"] == "succeeded"
    assert status["job"]["provider_task_id"] == "pred-1"
    assert status["job"]["output_assets"] == []


def test_finalize_is_idempotent(monkeypatch, tmp_path):
    tool = _load_tool(monkeypatch, tmp_path)

    monkeypatch.setattr(
        tool.registry,
        "dispatch",
        lambda *_args, **_kwargs: json.dumps(
            {
                "success": True,
                "video": "https://cdn.example/out.mp4",
                "provider": "atlas",
                "model": "wan-2.6-flash",
            }
        ),
    )

    created = json.loads(
        tool._handle_ultra_media_job_create(
            {"media_type": "video", "prompt": "animate", "auto_finalize": False}
        )
    )

    first = json.loads(tool._handle_ultra_media_job_finalize({"job_id": created["job_id"]}))
    second = json.loads(tool._handle_ultra_media_job_finalize({"job_id": created["job_id"]}))

    assert first["success"] is True
    assert second["success"] is True
    assert first["asset_id"] == second["asset_id"]
    assert len(second["job"]["output_assets"]) == 1
