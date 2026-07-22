import asyncio

import plugins.jobflow_inbox as jfi


def test_usage_help_when_no_url():
    reply = asyncio.run(jfi._handle_job(""))
    assert "/job <url>" in reply


def test_register_wires_job_command():
    captured = {}

    class FakeCtx:
        def register_command(self, name, handler, description="", args_hint=""):
            captured["name"] = name
            captured["handler"] = handler
            captured["args_hint"] = args_hint

    jfi.register(FakeCtx())
    assert captured["name"] == "job"
    assert captured["args_hint"] == "<url>"
    assert callable(captured["handler"])


from plugins.jobflow_inbox import ingest


def test_handler_returns_ingest_reply(monkeypatch):
    monkeypatch.setattr(
        ingest, "ingest_job",
        lambda raw, **kw: ingest.IngestResult("added", "✅ Queued *X* at *Y* ..."))
    reply = asyncio.run(jfi._handle_job("https://x.test/j/1"))
    assert reply.startswith("✅ Queued")


def test_handler_help_still_works_without_url():
    reply = asyncio.run(jfi._handle_job("   "))
    assert "/job <url>" in reply
