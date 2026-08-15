"""Single-attempt HTTP transport with redirects and retries disabled."""

from __future__ import annotations

import hashlib
import http.client
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit


class TransportError(RuntimeError):
    def __init__(self, code: str, *, request_may_have_arrived: bool) -> None:
        super().__init__(code)
        self.code = code
        self.request_may_have_arrived = request_may_have_arrived


@dataclass(frozen=True, slots=True)
class HttpResult:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    @property
    def body_sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


class SingleAttemptHttpTransport:
    def __init__(self, *, timeout_seconds: float = 20.0, max_response_bytes: int = 2_000_000) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
    ) -> HttpResult:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise TransportError("invalid_https_target", request_may_have_arrived=False)
        if parsed.fragment:
            raise TransportError("fragment_forbidden", request_may_have_arrived=False)
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        connection = http.client.HTTPSConnection(
            parsed.hostname,
            parsed.port or 443,
            timeout=self.timeout_seconds,
            context=ssl.create_default_context(),
        )
        sent = False
        try:
            connection.connect()
            connection.putrequest(method, target, skip_accept_encoding=True)
            for name, value in headers.items():
                connection.putheader(name, value)
            if body is not None:
                connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
            sent = True
            response = connection.getresponse()
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = response.read(min(64 * 1024, self.max_response_bytes + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > self.max_response_bytes:
                    raise TransportError("response_too_large", request_may_have_arrived=True)
                chunks.append(chunk)
            if 300 <= response.status < 400:
                raise TransportError("redirect_refused", request_may_have_arrived=sent)
            return HttpResult(
                status=int(response.status),
                headers=tuple((str(k), str(v)) for k, v in response.getheaders()),
                body=b"".join(chunks),
            )
        except TransportError:
            raise
        except (TimeoutError, OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise TransportError(type(exc).__name__, request_may_have_arrived=sent) from exc
        finally:
            connection.close()
