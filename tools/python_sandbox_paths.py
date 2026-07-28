"""Shared runtime-visible paths for python_sandbox datasets."""

from pathlib import PurePosixPath


PYTHON_SANDBOX_INPUTS_ROOT = PurePosixPath("/inputs")


def is_python_sandbox_dataset_name(name: str) -> bool:
    return bool(name) and all(
        char.isascii() and (char.isalnum() or char in "_-") for char in name
    )


def python_sandbox_dataset_path(name: str) -> PurePosixPath:
    """Return the path at which a configured dataset is visible in the jail."""
    if not is_python_sandbox_dataset_name(name):
        raise ValueError(f"invalid dataset name: {name!r}")
    return PYTHON_SANDBOX_INPUTS_ROOT / name
