import pytest

from cron import scheduler as sched


def _job(name="weekly-digest", job_id="job-123"):
    return {"name": name, "id": job_id}


def test_header_layout_puts_metadata_first_with_manage_hint():
    content = _wrap("body")
    # Metadata (name + id) comes first, content follows.
    assert content.index("Cronjob Response: weekly-digest") < content.index("body")
    assert "(job_id: job-123)" in content
    # The manage hint is preserved in header layout.
    assert "stop reminder weekly-digest" in content


def test_footer_layout_puts_content_first_with_job_id_trailing():
    content = _wrap("body", response_format="footer")
    # Content first, metadata last; the job id lands at the very trailing edge
    # so it is trivially extractable programmatically.
    assert content.index("body") < content.index("Cronjob Response: weekly-digest")
    assert content.rstrip().endswith("(job_id: job-123)")
    # The manage hint is present but never after the job id.
    assert "stop reminder weekly-digest" in content
    assert content.index("stop reminder") < content.index("(job_id: job-123)")


def test_wrap_response_false_returns_content_verbatim():
    assert _wrap("body", wrap_response=False) == "body"


def test_unknown_format_falls_back_to_header():
    # `_deliver_result` normalizes any value other than "footer" to "header"
    # (and logs a warning); the helper mirrors that so unknown values produce
    # the safe default rather than breaking output.
    for unknown in ("json", "foter", ""):
        content = _wrap("body", response_format=unknown)
        assert content.index("Cronjob Response: weekly-digest") < content.index("body")


def test_missing_job_name_falls_back_to_id():
    job = {"id": "job-abc"}
    content = sched._wrap_cron_response("body", job, True, "footer")
    assert "Cronjob Response: job-abc" in content
    assert content.rstrip().endswith("(job_id: job-abc)")


def _wrap(content, wrap_response=True, response_format="header"):
    return sched._wrap_cron_response(content, _job(), wrap_response, response_format)
