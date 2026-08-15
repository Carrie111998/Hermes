"""Least-authority GitHub Issues-only V1 publisher adapter."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from urllib.parse import quote

from hermes_cli.kanban_store.canonical import sha256_hex
from hermes_cli.kanban_store.reconciliation import ReconciliationResult
from hermes_cli.kanban_store.types import DispatchDisposition, DispatchOutcome

from .base import DispatchContract
from .http import HttpResult, SingleAttemptHttpTransport, TransportError

CredentialResolver = Callable[[str], str]


class GitHubIssuesAdapter:
    kind = "github.issues.v1"
    version = "github-issues-v1"

    def __init__(
        self,
        *,
        credential_resolver: CredentialResolver,
        api_base: str = "https://api.github.com",
        transport: SingleAttemptHttpTransport | None = None,
    ) -> None:
        self._credential_resolver = credential_resolver
        self._api_base = api_base.rstrip("/")
        self._transport = transport or SingleAttemptHttpTransport()

    def _headers(self, contract: DispatchContract) -> dict[str, str]:
        expected = dict(contract.application_headers)
        if expected.get("Authorization") != "Bearer <publisher-principal-credential>":
            raise ValueError("unrecognized GitHub authorization template")
        token = self._credential_resolver(contract.publisher_principal)
        if not token or "\n" in token or "\r" in token:
            raise ValueError("publisher credential is unavailable")
        expected["Authorization"] = f"Bearer {token}"
        expected["User-Agent"] = "hermes-kanban-publisher/1"
        return expected

    def _repository_path(self, target) -> str:
        owner = quote(str(target["owner"]), safe="")
        repo = quote(str(target["repo"]), safe="")
        return f"/repos/{owner}/{repo}"

    def _get(self, path: str, headers: dict[str, str]) -> HttpResult:
        return self._transport.request(
            method="GET", url=self._api_base + path, headers=headers, body=None
        )

    def _preflight(self, contract: DispatchContract, headers: dict[str, str]) -> None:
        repo_path = self._repository_path(contract.target)
        result = self._get(repo_path, headers)
        if result.status != 200:
            raise ValueError(f"repository_preflight_{result.status}")
        data = json.loads(result.body)
        if int(data.get("id", 0)) != int(contract.target["repository_id"]):
            raise ValueError("repository_numeric_identity_mismatch")
        if contract.kind == "github.issue.comment.create":
            number = int(contract.target["issue_number"])
            issue = self._get(f"{repo_path}/issues/{number}", headers)
            if issue.status != 200:
                raise ValueError(f"issue_preflight_{issue.status}")
            issue_data = json.loads(issue.body)
            if "pull_request" in issue_data:
                raise ValueError("pull_request_comment_forbidden")

    def dispatch(self, contract: DispatchContract) -> DispatchOutcome:
        if contract.adapter_version != self.version:
            return DispatchOutcome(
                DispatchDisposition.DEFINITE_NO_EFFECT, detail_code="adapter_version_mismatch"
            )
        if sha256_hex(contract.request_body_bytes) != contract.request_body_sha256:
            return DispatchOutcome(
                DispatchDisposition.DEFINITE_NO_EFFECT, detail_code="stored_body_digest_mismatch"
            )
        try:
            headers = self._headers(contract)
            self._preflight(contract, headers)
            repo_path = self._repository_path(contract.target)
            if contract.kind == "github.issue.create":
                path = repo_path + "/issues"
            elif contract.kind == "github.issue.comment.create":
                path = repo_path + f"/issues/{int(contract.target['issue_number'])}/comments"
            else:
                return DispatchOutcome(
                    DispatchDisposition.DEFINITE_NO_EFFECT, detail_code="unsupported_kind"
                )
            result = self._transport.request(
                method="POST",
                url=self._api_base + path,
                headers=headers,
                body=contract.request_body_bytes,
            )
        except ValueError as exc:
            return DispatchOutcome(
                DispatchDisposition.DEFINITE_NO_EFFECT, detail_code=str(exc)[:128]
            )
        except TransportError as exc:
            disposition = (
                DispatchDisposition.AMBIGUOUS
                if exc.request_may_have_arrived
                else DispatchDisposition.DEFINITE_NO_EFFECT
            )
            return DispatchOutcome(disposition, detail_code=exc.code)

        response_digest = result.body_sha256
        if 200 <= result.status < 300:
            try:
                data = json.loads(result.body)
                remote_identity = str(data.get("node_id") or data.get("id") or "")
            except Exception:
                return DispatchOutcome(
                    DispatchDisposition.AMBIGUOUS,
                    status_code=result.status,
                    detail_code="success_response_unparseable",
                    response_digest=response_digest,
                )
            if not remote_identity:
                return DispatchOutcome(
                    DispatchDisposition.AMBIGUOUS,
                    status_code=result.status,
                    detail_code="success_response_missing_identity",
                    response_digest=response_digest,
                )
            return DispatchOutcome(
                DispatchDisposition.SUCCESS,
                remote_identity=remote_identity,
                status_code=result.status,
                response_digest=response_digest,
            )
        if result.status in {400, 401, 403, 404, 405, 409, 410, 415, 422}:
            return DispatchOutcome(
                DispatchDisposition.DEFINITE_NO_EFFECT,
                status_code=result.status,
                detail_code=f"github_{result.status}",
                response_digest=response_digest,
            )
        return DispatchOutcome(
            DispatchDisposition.AMBIGUOUS,
            status_code=result.status,
            detail_code=f"github_{result.status}",
            response_digest=response_digest,
        )

    def _pages(self, path: str, headers: dict[str, str]) -> Iterator[HttpResult]:
        page = 1
        while page <= 100:
            separator = "&" if "?" in path else "?"
            result = self._get(f"{path}{separator}per_page=100&page={page}", headers)
            if result.status != 200:
                raise TransportError(f"lookup_{result.status}", request_may_have_arrived=False)
            yield result
            items = json.loads(result.body)
            if not isinstance(items, list) or len(items) < 100:
                return
            page += 1
        raise TransportError("pagination_bound_exceeded", request_may_have_arrived=False)

    def reconcile(self, contract: DispatchContract) -> ReconciliationResult:
        try:
            headers = self._headers(contract)
            self._preflight(contract, headers)
            repo_path = self._repository_path(contract.target)
            matches: list[dict[str, object]] = []
            if contract.kind == "github.issue.create":
                pages = self._pages(repo_path + "/issues?state=all&sort=created&direction=desc", headers)
                for page in pages:
                    for item in json.loads(page.body):
                        if "pull_request" in item:
                            continue
                        if contract.marker in str(item.get("body") or ""):
                            matches.append(
                                {
                                    "marker": contract.marker,
                                    "publisher_principal": contract.publisher_principal,
                                    "remote_identity": str(item.get("node_id") or item.get("id")),
                                    "number": int(item.get("number")),
                                }
                            )
            elif contract.kind == "github.issue.comment.create":
                number = int(contract.target["issue_number"])
                pages = self._pages(f"{repo_path}/issues/{number}/comments", headers)
                for page in pages:
                    for item in json.loads(page.body):
                        if contract.marker in str(item.get("body") or ""):
                            matches.append(
                                {
                                    "marker": contract.marker,
                                    "publisher_principal": contract.publisher_principal,
                                    "remote_identity": str(item.get("node_id") or item.get("id")),
                                    "issue_number": number,
                                }
                            )
            else:
                return ReconciliationResult(False, (), "unsupported_kind", {})
            return ReconciliationResult(True, tuple(matches), "complete_pagination", {})
        except (TransportError, ValueError, json.JSONDecodeError) as exc:
            return ReconciliationResult(
                False,
                (),
                type(exc).__name__,
                {"reason": str(exc)[:128]},
            )
