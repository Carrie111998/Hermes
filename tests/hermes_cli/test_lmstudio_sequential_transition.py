"""LM Studio load and sequential model-transition policy contracts."""

import json
from unittest.mock import patch

import pytest


class _Response:
    def __init__(self, payload=None):
        self.payload = payload or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def _model(key, *, loaded=None, max_context_length=262_144):
    return {
        "type": "llm",
        "key": key,
        "max_context_length": max_context_length,
        "loaded_instances": loaded or [],
    }


def _instance(instance_id, context_length=65_536):
    return {"id": instance_id, "config": {"context_length": context_length}}


def test_cold_load_receives_exact_context_and_verifies_echoed_result():
    from hermes_cli import models

    requests = []

    def urlopen(request, timeout):
        requests.append((request.full_url, json.loads(request.data), timeout))
        return _Response(
            {
                "status": "loaded",
                "instance_id": "large-local",
                "load_config": {"context_length": 65_536},
            }
        )

    with patch.object(
        models,
        "_lmstudio_fetch_raw_models",
        return_value=[_model("large-local")],
    ), patch.object(models, "_urlopen_model_catalog_request", side_effect=urlopen):
        loaded = models.ensure_lmstudio_model_loaded(
            "large-local",
            "http://localhost:1234/v1",
            "local-key",
            target_context_length=65_536,
            timeout=120,
            require_context_minimum=True,
        )

    assert loaded == 65_536
    assert requests == [
        (
            "http://localhost:1234/api/v1/models/load",
            {
                "model": "large-local",
                "context_length": 65_536,
                "echo_load_config": True,
            },
            120,
        )
    ]


def test_load_rejects_model_whose_maximum_is_below_required_minimum():
    from hermes_cli import models

    with patch.object(
        models,
        "_lmstudio_fetch_raw_models",
        return_value=[_model("small-context", max_context_length=32_768)],
    ), patch.object(models, "_urlopen_model_catalog_request") as urlopen:
        with pytest.raises(RuntimeError, match="65,536|65536"):
            models.ensure_lmstudio_model_loaded(
                "small-context",
                "http://localhost:1234/v1",
                None,
                target_context_length=65_536,
                require_context_minimum=True,
            )

    urlopen.assert_not_called()


def test_load_rejects_echoed_context_below_configured_minimum():
    from hermes_cli import models

    with patch.object(
        models,
        "_lmstudio_fetch_raw_models",
        return_value=[_model("large-local")],
    ), patch.object(
        models,
        "_urlopen_model_catalog_request",
        return_value=_Response(
            {
                "status": "loaded",
                "instance_id": "large-local",
                "load_config": {"context_length": 32_768},
            }
        ),
    ):
        with pytest.raises(RuntimeError, match="below|required"):
            models.ensure_lmstudio_model_loaded(
                "large-local",
                "http://localhost:1234/v1",
                None,
                target_context_length=65_536,
                require_context_minimum=True,
            )


def test_non_route_load_preserves_historical_context_clamp_and_payload():
    from hermes_cli import models

    requests = []

    def urlopen(request, timeout):
        requests.append(json.loads(request.data))
        return _Response()

    with patch.object(
        models,
        "_lmstudio_fetch_raw_models",
        return_value=[_model("small-context", max_context_length=32_768)],
    ), patch.object(models, "_urlopen_model_catalog_request", side_effect=urlopen):
        loaded = models.ensure_lmstudio_model_loaded(
            "small-context",
            "http://localhost:1234/v1",
            None,
            target_context_length=65_536,
        )

    assert loaded == 32_768
    assert requests == [{"model": "small-context", "context_length": 32_768}]


def test_sequential_policy_unloads_conflicting_llm_before_loading_target():
    from hermes_cli import models

    events = []

    def urlopen(request, timeout):
        payload = json.loads(request.data)
        if request.full_url.endswith("/unload"):
            events.append(("unload", payload["instance_id"], timeout))
            return _Response({"instance_id": payload["instance_id"]})
        assert payload["parallel"] == 1
        events.append(("load", payload["model"], payload["context_length"], timeout))
        return _Response(
            {
                "status": "loaded",
                "instance_id": payload["model"],
                "load_config": {
                    "context_length": payload["context_length"],
                    "parallel": payload["parallel"],
                },
            }
        )

    catalog = [
        _model("small-local", loaded=[_instance("small-local")]),
        _model("large-local"),
        {
            "type": "embedding",
            "key": "embedding-model",
            "loaded_instances": [_instance("embedding-model", 2_048)],
        },
    ]
    with patch.object(models, "_lmstudio_fetch_raw_models", return_value=catalog), patch.object(
        models, "_urlopen_model_catalog_request", side_effect=urlopen
    ):
        loaded = models.ensure_lmstudio_model_loaded(
            "large-local",
            "http://localhost:1234/v1",
            None,
            target_context_length=65_536,
            transition_policy="sequential",
            timeout=120,
        )

    assert loaded == 65_536
    assert events == [
        ("unload", "small-local", 30.0),
        ("load", "large-local", 65_536, 120),
    ]
    assert all("embedding-model" not in event for event in events)


def test_sequential_unload_failure_is_bounded_and_never_starts_target_load():
    from hermes_cli import models

    events = []

    def urlopen(request, timeout):
        payload = json.loads(request.data)
        events.append((request.full_url, payload, timeout))
        raise TimeoutError("bounded unload timeout")

    with patch.object(
        models,
        "_lmstudio_fetch_raw_models",
        return_value=[
            _model("small-local", loaded=[_instance("small-local")]),
            _model("large-local"),
        ],
    ), patch.object(models, "_urlopen_model_catalog_request", side_effect=urlopen):
        with pytest.raises(RuntimeError, match="unload"):
            models.ensure_lmstudio_model_loaded(
                "large-local",
                "http://localhost:1234/v1",
                None,
                target_context_length=65_536,
                transition_policy="sequential",
            )

    assert len(events) == 1
    assert events[0][0].endswith("/api/v1/models/unload")
    assert events[0][2] == 30.0


def test_failed_large_load_then_small_fallback_never_has_simultaneous_residency():
    from hermes_cli import models

    state: dict[str, str | None] = {"resident": "small-local"}
    events = []

    def catalog(**_kwargs):
        rows = []
        for key in ("large-local", "small-local"):
            loaded = [_instance(key)] if state["resident"] == key else []
            rows.append(_model(key, loaded=loaded))
        return rows

    def urlopen(request, timeout):
        payload = json.loads(request.data)
        if request.full_url.endswith("/unload"):
            events.append(("unload", payload["instance_id"]))
            assert state["resident"] == payload["instance_id"]
            state["resident"] = None
            return _Response({"instance_id": payload["instance_id"]})
        model = payload["model"]
        assert payload["parallel"] == 1
        events.append(("load", model))
        assert state["resident"] is None
        if model == "large-local":
            raise TimeoutError("large load failed")
        state["resident"] = model
        return _Response(
            {
                "status": "loaded",
                "instance_id": model,
                "load_config": {
                    "context_length": payload["context_length"],
                    "parallel": payload["parallel"],
                },
            }
        )

    with patch.object(models, "_lmstudio_fetch_raw_models", side_effect=catalog), patch.object(
        models, "_urlopen_model_catalog_request", side_effect=urlopen
    ):
        with pytest.raises(RuntimeError, match="load"):
            models.ensure_lmstudio_model_loaded(
                "large-local",
                "http://localhost:1234/v1",
                None,
                65_536,
                transition_policy="sequential",
            )
        assert state["resident"] is None
        assert models.ensure_lmstudio_model_loaded(
            "small-local",
            "http://localhost:1234/v1",
            None,
            65_536,
            transition_policy="sequential",
        ) == 65_536

    assert state["resident"] == "small-local"
    assert events == [
        ("unload", "small-local"),
        ("load", "large-local"),
        ("load", "small-local"),
    ]
