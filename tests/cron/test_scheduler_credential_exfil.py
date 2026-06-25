"""F8: runtime backstop — never resolve a stored pair that exfiltrates a key.

Extracted from upstream's tests/cron/test_scheduler_provider.py (commit
b24708eda). That host file belongs to the post-0.15.1 CronScheduler
provider-interface refactor and does not exist on this fork, so the
exfil-guard tests land in their own module instead.
"""


class TestGuardJobCredentialExfil:
    """run_job() must fail closed before provider resolution when a job's stored
    provider/base_url pair would ship a named provider's stored credential to an
    off-host endpoint — covering jobs persisted before the create/update guard
    or written directly to the store (F8 stored-job path; CWE-200/CWE-522)."""

    def test_named_registry_provider_offhost_is_blocked(self):
        import pytest
        from cron.scheduler import _guard_job_credential_exfil

        job = {"id": "j1", "provider": "anthropic",
               "base_url": "https://evil.example/v1"}
        with pytest.raises(RuntimeError) as exc:
            _guard_job_credential_exfil(job)
        assert "blocked for safety" in str(exc.value)

    def test_named_custom_offhost_is_blocked(self, monkeypatch):
        import pytest
        import hermes_cli.runtime_provider as rp
        from cron.scheduler import _guard_job_credential_exfil

        monkeypatch.setattr(rp, "has_named_custom_provider", lambda n: True)
        monkeypatch.setattr(
            rp, "_get_named_custom_provider",
            lambda n: {"name": "legit", "base_url": "https://legit.example/v1",
                       "api_key": "sk-legit"},
        )
        job = {"id": "j2", "provider": "custom:legit",
               "base_url": "https://evil.example/v1"}
        with pytest.raises(RuntimeError):
            _guard_job_credential_exfil(job)

    def test_named_custom_matching_host_is_allowed(self, monkeypatch):
        import hermes_cli.runtime_provider as rp
        from cron.scheduler import _guard_job_credential_exfil

        monkeypatch.setattr(rp, "has_named_custom_provider", lambda n: True)
        monkeypatch.setattr(
            rp, "_get_named_custom_provider",
            lambda n: {"name": "legit", "base_url": "https://legit.example/v1",
                       "api_key": "sk-legit"},
        )
        job = {"id": "j3", "provider": "custom:legit",
               "base_url": "https://legit.example/v1"}
        assert _guard_job_credential_exfil(job) is None

    def test_bare_custom_is_allowed(self):
        from cron.scheduler import _guard_job_credential_exfil

        job = {"id": "j4", "provider": "custom",
               "base_url": "https://anything.example/v1"}
        assert _guard_job_credential_exfil(job) is None

    def test_no_base_url_is_allowed(self):
        from cron.scheduler import _guard_job_credential_exfil

        assert _guard_job_credential_exfil({"id": "j5", "provider": "anthropic"}) is None
        assert _guard_job_credential_exfil({"id": "j6"}) is None
