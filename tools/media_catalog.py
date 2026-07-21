"""Media model catalog client — queries aiproxy the way the atlas CLI does.

Tool-layer dynamic model discovery. Replaces the static ``ATLAS_FAMILIES``
gate that used to live inside the atlas video-gen plugin: the tool layer now
asks the backend what models exist and what each one accepts, instead of
carrying a hand-maintained family table (which silently fell back to
``wan-2.6-flash`` for anything it did not know — see the改造 spec).

Mirrors ``atlas-cli-catalog-plan/internal/client`` (the reference the user
pointed at):

- list_models      → GET {root}/api/v1/catalog/models?type=...   (全类型目录)
- get_model_schema → GET {root}/v1/models/{id}, falling back to the static
                     OpenAPI schema at static.atlascloud.ai/model/schema/<slug>.json
- recommend        → heuristic scoring over list_models (atlas CLI v0.1 路径 A)

The backend address is config/env-driven: ``_default_credentials()`` reuses
the atlas plugin's ``resolve_credentials()`` (ATLAS_API_BASE / ATLAS_BASE_URL
env, ``providers.atlas.base_url`` config). Nothing here hardcodes an aiproxy
host — point that base_url at aiproxy and these queries route there.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

STATIC_SCHEMA_BASE = "https://static.atlascloud.ai/model/schema/"
DEFAULT_TIMEOUT_SECONDS = 30

# Typed error codes (mirror atlas CLI internal/client/errors.go closed set).
ERR_MODEL_NOT_FOUND = "model_not_found"
ERR_INVALID_PARAM = "invalid_param"
ERR_CATALOG_UNAVAILABLE = "catalog_unavailable"


class MediaCatalogError(Exception):
    """Structured catalog failure — never silently substituted with a default."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Model:
    """One catalog entry (mirrors aiproxy CatalogModel, flattened)."""

    id: str
    type: str = ""  # chat | image | video
    vendor: str = ""
    name: str = ""
    description: str = ""
    tags: List[str] = field(default_factory=list)
    input_modalities: List[str] = field(default_factory=list)
    output_modalities: List[str] = field(default_factory=list)
    supported_features: List[str] = field(default_factory=list)
    schema_url: str = ""
    pricing: Any = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ParamSchema:
    """One generation parameter (mirrors atlas CLI ParamSchema)."""

    type: str = ""  # string|integer|number|boolean|enum|array|url
    required: bool = False
    default: Any = None
    min: Optional[float] = None
    max: Optional[float] = None
    values: List[str] = field(default_factory=list)
    description: str = ""


@dataclass
class ModelSchema:
    """Full parameter schema for one model (mirrors atlas CLI ModelSchema)."""

    id: str
    type: str = ""
    vendor: str = ""
    description: str = ""
    params: Dict[str, ParamSchema] = field(default_factory=dict)


def _default_credentials() -> Tuple[str, str]:
    """Reuse the atlas plugin's config/env-driven backend address.

    Returns ``(api_key, api_root)`` where ``api_root`` has any trailing
    ``/v1`` stripped (so ``{root}/api/v1/...`` and ``{root}/v1/...`` are both
    correct). Single source of truth for the backend host — no hardcode here.
    """
    from plugins.video_gen.atlas.client import resolve_credentials

    return resolve_credentials()


def _infer_vendor(model_id: str) -> str:
    model_id = (model_id or "").strip()
    if not model_id:
        return ""
    return model_id.split("/", 1)[0].lower()


def _infer_type(model_id: str) -> str:
    lowered = (model_id or "").lower()
    if "video" in lowered:
        return "video"
    if "image" in lowered or "banana" in lowered:
        return "image"
    return ""


def _as_str_list(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v is not None and str(v) != ""]
    return []


def _parse_catalog_model(raw: Dict[str, Any]) -> Model:
    model_id = str(raw.get("id") or "").strip()
    return Model(
        id=model_id,
        type=str(raw.get("media_type") or raw.get("type") or "").strip().lower(),
        vendor=str(raw.get("vendor") or _infer_vendor(model_id)).strip().lower(),
        name=str(raw.get("name") or "").strip(),
        description=str(raw.get("description") or "").strip(),
        tags=_as_str_list(raw.get("tags")),
        input_modalities=_as_str_list(raw.get("input_modalities")),
        output_modalities=_as_str_list(raw.get("output_modalities")),
        supported_features=_as_str_list(raw.get("supported_features")),
        schema_url=str(raw.get("schema_url") or "").strip(),
        pricing=raw.get("pricing"),
        raw=raw,
    )


def _static_enum_values(values: Any) -> List[str]:
    out: List[str] = []
    if not isinstance(values, (list, tuple)):
        return out
    for item in values:
        if isinstance(item, bool):
            out.append("true" if item else "false")
        elif isinstance(item, float) and item.is_integer():
            out.append(str(int(item)))
        elif item is not None:
            out.append(str(item))
    return out


def _parse_param(raw: Dict[str, Any], required: bool) -> ParamSchema:
    """Parse one parameter from either schema convention.

    The static OpenAPI schema uses ``minimum/maximum/enum``; the raw
    atlas-CLI ModelSchema body uses ``min/max/values``. Read both so one
    helper serves the ``/v1/models/<id>`` body and the static fallback.
    """
    typ = raw.get("type")
    if isinstance(typ, list):  # OpenAPI union type, e.g. ["string","null"]
        typ = next((t for t in typ if isinstance(t, str) and t != "null"), "")
    typ = str(typ or "")
    enum = raw.get("values") or raw.get("enum")
    if not typ and enum:
        typ = "enum"
    min_value = raw.get("min", raw.get("minimum"))
    max_value = raw.get("max", raw.get("maximum"))
    return ParamSchema(
        type=typ,
        required=required,
        default=raw.get("default"),
        min=min_value if isinstance(min_value, (int, float)) else None,
        max=max_value if isinstance(max_value, (int, float)) else None,
        values=_static_enum_values(enum),
        description=str(raw.get("description") or ""),
    )


class MediaCatalogClient:
    """Queries the backend catalog + per-model schema (atlas CLI pattern)."""

    def __init__(
        self,
        api_root: Optional[str] = None,
        api_key: Optional[str] = None,
        *,
        http: Optional[httpx.Client] = None,
        static_schema_base: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ):
        if api_root is None or api_key is None:
            key, root = _default_credentials()
            self.api_root = (api_root or root or "").rstrip("/")
            self.api_key = api_key if api_key is not None else key
        else:
            self.api_root = api_root.rstrip("/")
            self.api_key = api_key
        self._http = http  # injectable for tests (httpx.MockTransport)
        self.static_schema_base = (static_schema_base or STATIC_SCHEMA_BASE).rstrip("/") + "/"
        self.timeout = timeout

    # -- HTTP plumbing ------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "User-Agent": "hermes-agent/media_catalog",
            "Authorization": f"Bearer {self.api_key}",
        }

    def _get(self, url: str, *, params: Optional[Dict[str, Any]] = None) -> httpx.Response:
        if self._http is not None:
            return self._http.get(url, headers=self._headers(), params=params, timeout=self.timeout)
        with httpx.Client() as client:
            return client.get(url, headers=self._headers(), params=params, timeout=self.timeout)

    # -- Discovery ----------------------------------------------------------

    def list_models(self, type: Optional[str] = None) -> List[Model]:
        """List catalog models. type ∈ chat|image|video (None = all)."""
        url = f"{self.api_root}/api/v1/catalog/models"
        params = {"type": type} if type else None
        try:
            resp = self._get(url, params=params)
        except httpx.HTTPError as exc:
            raise MediaCatalogError(ERR_CATALOG_UNAVAILABLE, f"catalog request failed: {exc}") from exc
        if resp.status_code == 404:
            raise MediaCatalogError(
                ERR_CATALOG_UNAVAILABLE,
                "backend has no /api/v1/catalog/models endpoint; point base_url at an "
                "aiproxy build that serves the model catalog",
            )
        if resp.status_code >= 400:
            raise MediaCatalogError(
                ERR_CATALOG_UNAVAILABLE,
                f"catalog returned HTTP {resp.status_code}: {resp.text[:300]}",
            )
        body = resp.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise MediaCatalogError(ERR_CATALOG_UNAVAILABLE, "catalog response missing data list")
        models = [_parse_catalog_model(item) for item in data if isinstance(item, dict)]
        if type:
            wanted = type.strip().lower()
            models = [m for m in models if not m.type or m.type == wanted]
        return models

    def model_exists(self, model_id: str, *, type: Optional[str] = None) -> bool:
        """True when the catalog lists this model id (dynamic membership check)."""
        wanted = (model_id or "").strip()
        if not wanted:
            return False
        try:
            return any(m.id == wanted for m in self.list_models(type=type))
        except MediaCatalogError:
            # Catalog unreachable — do NOT fake membership; let the caller
            # surface a structured error rather than guess.
            raise

    # -- Per-model schema ---------------------------------------------------

    def get_model_schema(self, model_id: str) -> ModelSchema:
        """Full parameter schema for a model.

        Tries ``GET /v1/models/{id}`` first; aiproxy answers that with an
        OpenAI-style envelope (no params), so when no params come back we fall
        through to the static OpenAPI schema — the real constraint source for
        video/image models.
        """
        wanted = (model_id or "").strip()
        if not wanted:
            raise MediaCatalogError(ERR_INVALID_PARAM, "model id is required")
        try:
            resp = self._get(f"{self.api_root}/v1/models/{wanted}")
            if resp.status_code == 200:
                schema = self._parse_model_schema_body(wanted, resp.json())
                if schema.params:
                    return schema
        except httpx.HTTPError as exc:
            logger.debug("v1/models/%s lookup failed, trying static schema: %s", wanted, exc)
        return self._static_model_schema(wanted)

    def _parse_model_schema_body(self, model_id: str, body: Any) -> ModelSchema:
        """Parse a raw ModelSchema body (atlas CLI shape). Returns empty params
        for aiproxy's {code,msg,data} envelope so the caller falls to static."""
        if not isinstance(body, dict):
            return ModelSchema(id=model_id)
        params_raw = body.get("params")
        if not isinstance(params_raw, dict):
            return ModelSchema(id=model_id)
        params: Dict[str, ParamSchema] = {}
        for name, raw in params_raw.items():
            if name == "model" or not isinstance(raw, dict):
                continue
            params[name] = _parse_param(raw, bool(raw.get("required")))
        return ModelSchema(
            id=model_id,
            type=str(body.get("type") or _infer_type(model_id)),
            vendor=str(body.get("vendor") or _infer_vendor(model_id)),
            description=str(body.get("description") or ""),
            params=params,
        )

    def _static_model_schema(self, model_id: str) -> ModelSchema:
        slug = model_id.replace("/", "-")
        url = f"{self.static_schema_base}{quote(slug)}.json"
        try:
            resp = self._get(url)
        except httpx.HTTPError as exc:
            raise MediaCatalogError(
                ERR_MODEL_NOT_FOUND,
                f"model {model_id!r}: schema lookup failed ({exc}). Call models_list to "
                f"discover available models.",
            ) from exc
        if resp.status_code == 404:
            raise MediaCatalogError(
                ERR_MODEL_NOT_FOUND,
                f"model {model_id!r} does not exist. Call models_list or models_search to "
                f"discover available models.",
            )
        if resp.status_code >= 400:
            raise MediaCatalogError(
                ERR_MODEL_NOT_FOUND,
                f"model {model_id!r}: schema returned HTTP {resp.status_code}.",
            )
        return self._parse_static_schema(model_id, resp.json())

    def _parse_static_schema(self, model_id: str, body: Any) -> ModelSchema:
        """Parse OpenAPI components.schemas.Input.{required,properties}."""
        try:
            input_schema = body["components"]["schemas"]["Input"]
        except (KeyError, TypeError) as exc:
            raise MediaCatalogError(
                ERR_MODEL_NOT_FOUND,
                f"model {model_id!r}: static schema missing components.schemas.Input.",
            ) from exc
        props = input_schema.get("properties") if isinstance(input_schema, dict) else None
        if not isinstance(props, dict) or not props:
            raise MediaCatalogError(
                ERR_MODEL_NOT_FOUND,
                f"model {model_id!r}: static schema has no Input.properties.",
            )
        required = {
            str(name)
            for name in (input_schema.get("required") or [])
        }
        params: Dict[str, ParamSchema] = {}
        for name, raw in props.items():
            if name == "model" or not isinstance(raw, dict):
                continue
            params[name] = _parse_param(raw, name in required)
        return ModelSchema(
            id=model_id,
            type=_infer_type(model_id),
            vendor=_infer_vendor(model_id),
            params=params,
        )

    # -- Recommendation (atlas CLI v0.1 heuristic, 路径 A) -------------------

    def recommend(
        self,
        task: str,
        *,
        type: Optional[str] = None,
        limit: int = 5,
    ) -> List[Tuple[Model, str]]:
        """Rank catalog models for a described task. Returns (model, reason)."""
        models = self.list_models(type=type)
        task_words = [w for w in (task or "").lower().split() if len(w) >= 3]
        scored: List[Tuple[float, Model, List[str]]] = []
        for model in models:
            hay = " ".join([model.id, model.description, " ".join(model.tags)]).lower()
            reasons: List[str] = []
            score = 0.0
            for word in task_words:
                if word in hay:
                    score += 0.3
                    reasons.append(f"matches {word!r}")
            for tag in model.tags:
                lowered = tag.lower()
                if lowered == "recommended":
                    score += 0.2
                    reasons.append("default recommendation")
                elif lowered in {"reference", "identity", "reference-to-video"}:
                    score += 0.2
                    reasons.append(f"tagged {tag!r}")
            if score > 0:
                scored.append((score, model, reasons))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [(model, "; ".join(dict.fromkeys(reasons))) for _, model, reasons in scored[:limit]]


_default_client: Optional[MediaCatalogClient] = None


def get_catalog_client() -> MediaCatalogClient:
    """Process-wide catalog client (lazy; reuses configured backend address)."""
    global _default_client
    if _default_client is None:
        _default_client = MediaCatalogClient()
    return _default_client


def _reset_client_for_tests() -> None:
    global _default_client
    _default_client = None
