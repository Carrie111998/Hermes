"""Persistent-overlay path safety for the Singularity environment.

A raw task id used as an overlay directory name could carry characters
that break downstream consumers of the path (bind specs split on ``:``);
overlay components must go through the shared path-component encoder.
"""

from tools.environments import singularity as singularity_env
from tools.environments.base import safe_path_component


def test_persistent_overlay_dir_encodes_colon_in_task_id(monkeypatch, tmp_path):
    monkeypatch.setattr(
        singularity_env, "_ensure_singularity_available", lambda: "/usr/bin/apptainer"
    )
    monkeypatch.setattr(
        singularity_env,
        "_get_or_build_sif",
        lambda image, executable="apptainer": str(tmp_path / "image.sif"),
    )
    monkeypatch.setattr(singularity_env, "_get_scratch_dir", lambda: tmp_path / "scratch")
    monkeypatch.setattr(
        singularity_env.SingularityEnvironment, "_start_instance", lambda self: None
    )
    monkeypatch.setattr(
        singularity_env.SingularityEnvironment, "init_session", lambda self: None
    )

    task_id = "session:20260822_221751_75e446"
    env = singularity_env.SingularityEnvironment(
        image="python:3.11",
        persistent_filesystem=True,
        task_id=task_id,
    )

    assert env._overlay_dir is not None
    assert env._overlay_dir.name == f"overlay-{safe_path_component(task_id)}"
    assert ":" not in env._overlay_dir.name
    assert env._overlay_dir.is_dir()
