"""Tests for the frozen HL-AOS classification taint source module."""

from types import SimpleNamespace

from agent.hl_aos_classification import (
    read_hl_aos_classification,
    classification_source,
    HL_AOS_TAINT_ATTR,
    HL_AOS_METADATA_KEY,
    HL_AOS_TAINT_SOURCE_LABEL,
    HL_AOS_UNCLASSIFIED_LABEL,
)


def test_reads_classification_from_frozen_agent_attribute():
    agent = SimpleNamespace()
    setattr(agent, HL_AOS_TAINT_ATTR, "C2_LOCAL_ONLY")
    assert read_hl_aos_classification(agent) == "C2_LOCAL_ONLY"


def test_reads_classification_from_runtime_metadata():
    agent = SimpleNamespace(runtime_metadata={HL_AOS_METADATA_KEY: "C0_PUBLIC"})
    assert read_hl_aos_classification(agent) == "C0_PUBLIC"


def test_agent_attribute_takes_priority_over_metadata():
    agent = SimpleNamespace(
        runtime_metadata={HL_AOS_METADATA_KEY: "C0_PUBLIC"}
    )
    setattr(agent, HL_AOS_TAINT_ATTR, "C3_RESTRICTED")
    assert read_hl_aos_classification(agent) == "C3_RESTRICTED"


def test_returns_empty_string_when_no_taint_present():
    agent = SimpleNamespace()
    assert read_hl_aos_classification(agent) == ""


def test_returns_empty_string_for_empty_attribute():
    agent = SimpleNamespace()
    setattr(agent, HL_AOS_TAINT_ATTR, "")
    assert read_hl_aos_classification(agent) == ""


def test_returns_empty_string_for_whitespace_attribute():
    agent = SimpleNamespace()
    setattr(agent, HL_AOS_TAINT_ATTR, "   ")
    assert read_hl_aos_classification(agent) == ""


def test_returns_empty_string_when_metadata_has_none():
    agent = SimpleNamespace(runtime_metadata={HL_AOS_METADATA_KEY: None})
    assert read_hl_aos_classification(agent) == ""


def test_returns_empty_string_for_legacy_a1_classification_attribute():
    """Legacy attribute `a1_classification` must be ignored."""
    agent = SimpleNamespace()
    setattr(agent, "a1_classification", "C2_LOCAL_ONLY")
    assert read_hl_aos_classification(agent) == ""


def test_strips_and_uppercases_classification():
    agent = SimpleNamespace()
    setattr(agent, HL_AOS_TAINT_ATTR, "  c2_local_only  ")
    assert read_hl_aos_classification(agent) == "C2_LOCAL_ONLY"


def test_classification_source_returns_frozen_label_when_taint_present():
    agent = SimpleNamespace()
    setattr(agent, HL_AOS_TAINT_ATTR, "C0_PUBLIC")
    assert classification_source(agent) == HL_AOS_TAINT_SOURCE_LABEL
    assert classification_source(agent) == "hl_aos_frozen"


def test_classification_source_returns_unclassified_when_no_taint():
    agent = SimpleNamespace()
    assert classification_source(agent) == HL_AOS_UNCLASSIFIED_LABEL
    assert classification_source(agent) == "unclassified"


def test_classification_source_returns_unclassified_for_legacy_attribute():
    """Legacy `a1_classification` must not count as a certified taint source."""
    agent = SimpleNamespace()
    setattr(agent, "a1_classification", "C0_PUBLIC")
    assert classification_source(agent) == "unclassified"
