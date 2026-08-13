"""OAG boundary: fail-closed identity verification and tenant admission.

The Access Gateway (OAG) is the only place external identity evidence
(a signed JWT) is turned into a :class:`~enterprise.contracts.VerifiedIdentity`.
It is deliberately paranoid:

* the algorithm must be exactly the configured one (``none`` and alg
  confusion are rejected before any signature work),
* HS256 verification is stdlib-only and constant-time,
* RS256 verification requires the optional ``cryptography`` package — when
  it is unavailable the verifier raises instead of skipping verification,
* the installation ALWAYS comes from server-side :class:`TrustConfig`;
  caller-supplied claims can never select it,
* namespace admission resolves the configured tenant claim through the
  server-side ``tenant_map`` and requires an exact match,
* every failure raises :class:`~enterprise.errors.AdmissionError` with a
  safe reason that never echoes token contents.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any

from .contracts import IdentityVerifier, VerifiedIdentity
from .errors import AdmissionError

_LEEWAY_SECONDS = 30
_SUPPORTED_ALGS = ("HS256", "RS256")


@dataclass(frozen=True)
class TrustConfig:
    """Server-side trust anchors for the OAG boundary.

    Nothing in here is caller-influenced: the installation, accepted
    issuer/audience, verification key material, and the tenant->namespace
    map are all operator configuration.
    """

    issuer: str
    audience: str
    installation: str
    algorithm: str = "HS256"
    hs256_secret: str | None = None
    rs256_public_keys: dict[str, str] = field(default_factory=dict)  # kid -> PEM
    required_claims: dict[str, Any] = field(default_factory=dict)
    tenant_claim: str = "org"
    tenant_map: dict[str, str] = field(default_factory=dict)  # tenant value -> namespace
    leeway_seconds: int = _LEEWAY_SECONDS


def _b64url_decode(segment: str) -> bytes:
    pad = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + pad)
    except (binascii.Error, ValueError) as exc:
        raise AdmissionError("token is not valid base64url") from exc


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_json_segment(segment: str, what: str) -> dict[str, Any]:
    raw = _b64url_decode(segment)
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise AdmissionError(f"token {what} is not valid JSON") from exc
    if not isinstance(obj, dict):
        raise AdmissionError(f"token {what} is not a JSON object")
    return obj


class OAGVerifier(IdentityVerifier):
    """Stdlib-only JWT verifier implementing the OAG admission boundary."""

    name = "oag"

    def __init__(self, config: TrustConfig) -> None:
        if config.algorithm not in _SUPPORTED_ALGS:
            raise AdmissionError("unsupported configured algorithm")
        if config.algorithm == "HS256" and not config.hs256_secret:
            raise AdmissionError("hs256 secret not configured")
        if config.algorithm == "RS256" and not config.rs256_public_keys:
            raise AdmissionError("rs256 public keys not configured")
        self._config = config

    # -- IdentityVerifier -------------------------------------------------

    def verify(self, token: str, *, require_namespace: str | None = None) -> VerifiedIdentity:
        cfg = self._config
        header, claims, signing_input, signature = self._split(token)
        self._check_alg(header)
        self._check_signature(header, signing_input, signature)
        self._check_claims(claims)

        namespace: str | None = None
        if require_namespace is not None:
            namespace = self._admit_namespace(claims, require_namespace)

        return VerifiedIdentity(
            issuer=cfg.issuer,
            subject=str(claims["sub"]),
            installation=cfg.installation,  # server-selected, never from claims
            namespace=namespace,
            claims=dict(claims),
        )

    def admit(self, token: str, namespace: str | None = None) -> VerifiedIdentity:
        """Convenience entry point mirroring the gateway request shape."""
        return self.verify(token, require_namespace=namespace)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _split(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
        if not isinstance(token, str) or not token:
            raise AdmissionError("token missing")
        parts = token.split(".")
        if len(parts) != 3:
            raise AdmissionError("token is not a compact JWS")
        header = _decode_json_segment(parts[0], "header")
        claims = _decode_json_segment(parts[1], "payload")
        signature = _b64url_decode(parts[2])
        signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
        return header, claims, signing_input, signature

    def _check_alg(self, header: dict[str, Any]) -> None:
        alg = header.get("alg")
        if alg != self._config.algorithm:
            # Rejects 'none', missing alg, and any alg-confusion attempt.
            raise AdmissionError("token algorithm not permitted")

    def _check_signature(self, header: dict[str, Any], signing_input: bytes,
                         signature: bytes) -> None:
        cfg = self._config
        if not signature:
            raise AdmissionError("token signature missing")
        if cfg.algorithm == "HS256":
            secret = cfg.hs256_secret
            if not secret:
                raise AdmissionError("hs256 secret not configured")
            expected = hmac.new(secret.encode("utf-8"), signing_input,
                                hashlib.sha256).digest()
            if not hmac.compare_digest(expected, signature):
                raise AdmissionError("token signature invalid")
            return
        # RS256: only with the optional 'cryptography' package. Verification
        # is never skipped — absence of the package is a denial.
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding, rsa
        except ImportError as exc:  # pragma: no cover - env dependent
            raise AdmissionError("rs256 unavailable") from exc
        kid = header.get("kid")
        pem = cfg.rs256_public_keys.get(kid) if isinstance(kid, str) else None
        if pem is None:
            raise AdmissionError("no verification key for token")
        try:
            key = serialization.load_pem_public_key(pem.encode("utf-8"))
            if not isinstance(key, rsa.RSAPublicKey):
                raise AdmissionError("verification key unusable")
            key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature as exc:
            raise AdmissionError("token signature invalid") from exc
        except (ValueError, TypeError, AttributeError) as exc:
            raise AdmissionError("verification key unusable") from exc

    def _check_claims(self, claims: dict[str, Any]) -> None:
        cfg = self._config
        now = time.time()
        leeway = max(0, int(cfg.leeway_seconds))

        if claims.get("iss") != cfg.issuer:
            raise AdmissionError("issuer not accepted")

        aud = claims.get("aud")
        aud_values = aud if isinstance(aud, list) else [aud]
        if cfg.audience not in aud_values:
            raise AdmissionError("audience not accepted")

        exp = claims.get("exp")
        if not isinstance(exp, (int, float)) or isinstance(exp, bool):
            raise AdmissionError("expiry missing or malformed")
        if now > exp + leeway:
            raise AdmissionError("token expired")

        nbf = claims.get("nbf")
        if nbf is not None:
            if not isinstance(nbf, (int, float)) or isinstance(nbf, bool):
                raise AdmissionError("not-before malformed")
            if now < nbf - leeway:
                raise AdmissionError("token not yet valid")

        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub.strip():
            raise AdmissionError("subject missing")

        for name, expected in cfg.required_claims.items():
            if name not in claims or claims[name] != expected:
                raise AdmissionError("required claim not satisfied")

    def _admit_namespace(self, claims: dict[str, Any], required: str) -> str:
        cfg = self._config
        tenant = claims.get(cfg.tenant_claim)
        if not isinstance(tenant, str) or not tenant:
            raise AdmissionError("tenant claim missing")
        mapped = cfg.tenant_map.get(tenant)
        if mapped is None:
            raise AdmissionError("tenant not admitted to any namespace")
        if mapped != required:
            raise AdmissionError("tenant not admitted to requested namespace")
        return required


# ---------------------------------------------------------------------------
# TEST SUPPORT — not for production token issuance.
#
# mint_test_token exists so tests and local dev setups can produce HS256
# tokens against a TrustConfig. Production tokens come from the real
# identity provider; nothing in the platform calls this at runtime.
# ---------------------------------------------------------------------------


def mint_test_token(config: TrustConfig, claims: dict[str, Any],
                    key: str, *, header: dict[str, Any] | None = None) -> str:
    """Mint an HS256 JWT for tests/dev. HS256 only, by design."""
    hdr = {"alg": "HS256", "typ": "JWT"}
    if header:
        hdr.update(header)
    payload: dict[str, Any] = {
        "iss": config.issuer,
        "aud": config.audience,
        "exp": int(time.time()) + 300,
    }
    payload.update(claims)
    h = _b64url_encode(json.dumps(hdr, separators=(",", ":")).encode("utf-8"))
    p = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{h}.{p}".encode("ascii")
    sig = hmac.new(key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url_encode(sig)}"
