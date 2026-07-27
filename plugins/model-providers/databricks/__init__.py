"""Databricks provider profile.

Databricks Model Serving and Unity AI Gateway expose an OpenAI-compatible
Chat Completions API, authenticated via Personal Access Token (PAT) or OAuth.

Key concepts:
  - **Workspace URL**: Every Databricks customer has their own workspace URL
    (e.g. ``https://dbc-XXXX.cloud.databricks.com``). Users configure this as
    the ``base_url`` in their Hermes model config, appending ``/v1`` for the
    OpenAI-compatible path, e.g. ``https://dbc-XXXX.cloud.databricks.com/v1``.
  - **Serving endpoints**: Models are deployed as named serving endpoints.
    The endpoint name is used as the ``model`` parameter in Chat Completions
    requests. The provider fetches the available endpoints via the Databricks
    REST API (``GET /api/2.0/serving-endpoints``).
  - **Authentication**: Databricks PATs are sent as ``Bearer <token>`` in
    the Authorization header, which matches the default ``fetch_models``
    Bearer auth pattern.
"""

from __future__ import annotations

import json
import logging
import urllib.request

from providers import register_provider
from providers.base import ProviderProfile, _profile_user_agent

logger = logging.getLogger(__name__)


def _strip_v1_suffix(url: str) -> str:
    """Remove a trailing ``/v1`` from a base URL to get the workspace root.

    Users typically configure ``base_url`` as
    ``https://<workspace>/v1`` for the OpenAI-compatible endpoint.
    The Databricks REST API (``/api/2.0/serving-endpoints``) lives
    on the workspace root without ``/v1``.
    """
    stripped = url.rstrip("/")
    if stripped.endswith("/v1"):
        return stripped[:-3].rstrip("/")
    return stripped


class DatabricksProfile(ProviderProfile):
    """Databricks Model Serving / Unity AI Gateway — OpenAI-compatible API."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Fetch available model serving endpoints from the Databricks workspace.

        Calls ``GET /api/2.0/serving-endpoints`` on the workspace root URL
        (derived by stripping the ``/v1`` suffix from the inference base URL).

        Returns the list of serving endpoint names, or ``None`` if the
        endpoint is unreachable or credentials are missing.
        """
        if not api_key:
            return None

        effective_base = base_url or self.base_url
        if not effective_base:
            return None

        # Databricks REST API lives at the workspace root, not under /v1.
        ws_url = _strip_v1_suffix(effective_base)
        url = ws_url + "/api/2.0/serving-endpoints"

        try:
            req = urllib.request.Request(url)
            req.add_header("Authorization", f"Bearer {api_key}")
            req.add_header("Accept", "application/json")
            req.add_header("User-Agent", _profile_user_agent())

            # Lazy import so unittest.mock.patch intercepts the call
            from hermes_cli.urllib_security import open_credentialed_url

            with open_credentialed_url(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())

            # Response is either a list of endpoint objects directly, or
            # wrapped in an "endpoints" key (the Databricks SDK pagination
            # model).  Handle both shapes.
            endpoints = data if isinstance(data, list) else data.get("endpoints", [])

            return [
                ep["name"]
                for ep in endpoints
                if isinstance(ep, dict) and "name" in ep
            ]
        except Exception as exc:
            logger.debug("fetch_models(databricks): %s", exc)
            return None


databricks = DatabricksProfile(
    name="databricks",
    aliases=(
        "databricks-serving",
        "databricks-gateway",
        "dbx",
    ),
    display_name="Databricks",
    description="Databricks — Model Serving / Unity AI Gateway (OpenAI-compatible)",
    signup_url="https://accounts.cloud.databricks.com/",
    env_vars=(
        "DATABRICKS_TOKEN",
        "DATABRICKS_BASE_URL",
    ),
    auth_type="api_key",
    supports_vision=False,
    supports_health_check=True,
)

register_provider(databricks)
