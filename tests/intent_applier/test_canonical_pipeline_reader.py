import json
from pathlib import Path

from intent_applier.canonical_pipeline_reader import (
    build_default_canonical_reader,
    load_canonical_business_states,
)


def _write(path: Path, obj) -> Path:
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def test_dict_shaped_jobs_maps_job_id_to_current_business_state(tmp_path):
    p = _write(tmp_path / "pipeline.json", {
        "jobs": {
            "job-a": {"stage": "review", "currentBusinessState": "materials_ready"},
            "job-b": {"stage": "archived", "currentBusinessState": "archived"},
        }
    })
    assert load_canonical_business_states(p) == {
        "job-a": "materials_ready",
        "job-b": "archived",
    }


def test_jobs_without_current_business_state_are_omitted(tmp_path):
    p = _write(tmp_path / "pipeline.json", {
        "jobs": {
            "job-a": {"stage": "review"},                       # no currentBusinessState
            "job-b": {"currentBusinessState": ""},              # empty -> omit
            "job-c": {"currentBusinessState": "submitted"},     # kept
        }
    })
    assert load_canonical_business_states(p) == {"job-c": "submitted"}


def test_legacy_list_shaped_jobs_yields_empty_map(tmp_path):
    # The PipelineManager legacy projection uses jobs=list; gate B must ignore it.
    p = _write(tmp_path / "pipeline.json", {"jobs": [{"job_id": "x", "stage": "review"}]})
    assert load_canonical_business_states(p) == {}


def test_missing_or_corrupt_file_yields_empty_map(tmp_path):
    assert load_canonical_business_states(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_canonical_business_states(bad) == {}


def test_build_default_reader_is_zero_arg_and_reads_the_bound_path(tmp_path):
    p = _write(tmp_path / "pipeline.json", {
        "jobs": {"job-a": {"currentBusinessState": "offer"}}
    })
    reader = build_default_canonical_reader(p)
    assert reader() == {"job-a": "offer"}
