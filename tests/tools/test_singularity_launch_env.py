"""Tests for the apptainer sandbox launch-env sanitization.

The backend process env can carry host-only values — ``SSL_CERT_FILE``
pointing at the venv's certifi bundle, a launch cwd like
``/usr/local/lib/hermes-agent`` — that do not exist inside the sandbox.
That kills HTTPS (curl error 77) and makes every ``cd`` fail. These helpers
rewrite the launch env to a container-visible CA bundle and launch from a
container-valid cwd, mirroring the Docker backend's CA rewrite.
"""

import os
from unittest.mock import patch

from tools.environments.singularity import (
    _SANDBOX_LAUNCH_CWD,
    _container_ca_bundle,
    _sandbox_launch_env,
)


class TestContainerCaBundle:
    def test_prefers_system_bundle(self):
        with patch("os.path.exists", return_value=True):
            assert _container_ca_bundle() == "/etc/ssl/certs/ca-certificates.crt"

    def test_falls_back_to_inherited_cert_when_system_missing(self):
        def exists(path):
            return path == "/custom/ca.pem"

        with patch.dict(os.environ, {"SSL_CERT_FILE": "/custom/ca.pem"}, clear=False), \
             patch("os.path.exists", side_effect=exists):
            assert _container_ca_bundle() == "/custom/ca.pem"

    def test_ignores_stale_inherited_cert(self):
        with patch.dict(os.environ, {"SSL_CERT_FILE": "/host-only/venv/cacert.pem"}, clear=False), \
             patch("os.path.exists", return_value=False):
            # Nothing exists on the host: must still return a non-empty
            # fallback (certifi / canonical path), never the dead path.
            result = _container_ca_bundle()
            assert result
            assert result != "/host-only/venv/cacert.pem"


class TestSandboxLaunchEnv:
    def test_rewrites_ca_env_vars_to_container_visible_path(self):
        with patch("os.path.exists", return_value=True):
            env = _sandbox_launch_env()
        ca = "/etc/ssl/certs/ca-certificates.crt"
        assert env["SSL_CERT_FILE"] == ca
        assert env["CURL_CA_BUNDLE"] == ca
        assert env["REQUESTS_CA_BUNDLE"] == ca

    def test_preserves_other_env(self):
        with patch.dict(os.environ, {"SOME_VAR": "value"}, clear=False), \
             patch("os.path.exists", return_value=True):
            env = _sandbox_launch_env()
        assert env["SOME_VAR"] == "value"

    def test_launch_cwd_is_container_visible_root(self):
        assert _SANDBOX_LAUNCH_CWD == "/root"
