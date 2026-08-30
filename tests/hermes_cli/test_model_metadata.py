"""Tests for hermes_cli/model_metadata.py — model metadata catalog."""


def test_model_metadata_import():
    from hermes_cli.model_metadata import MODEL_METADATA
    assert isinstance(MODEL_METADATA, dict)
    assert len(MODEL_METADATA) > 0
