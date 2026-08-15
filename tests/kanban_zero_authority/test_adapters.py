from __future__ import annotations

import json
import unittest

from hermes_cli.kanban_publisher.base import DispatchContract
from hermes_cli.kanban_publisher.github import GitHubIssuesAdapter
from hermes_cli.kanban_publisher.http import HttpResult, TransportError
from hermes_cli.kanban_store.canonical import canonical_json_bytes, sha256_hex
from hermes_cli.kanban_store.types import DispatchDisposition


class QueueTransport:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def result(status, value):
    return HttpResult(status=status, headers=(), body=json.dumps(value).encode())


def contract(kind="github.issue.create", issue_number=None):
    body = canonical_json_bytes({"title": "T", "body": "B <!-- hermes -->"}) if kind.endswith("issue.create") else canonical_json_bytes({"body": "B <!-- hermes -->"})
    target = {"repository_id": 1, "owner": "NousResearch", "repo": "hermes-agent"}
    if issue_number is not None:
        target["issue_number"] = issue_number
    return DispatchContract(
        dispatch_id="d",
        intent_id="i",
        kind=kind,
        publisher_principal="app:1",
        adapter_version="github-issues-v1",
        target=target,
        application_headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer <publisher-principal-credential>",
            "Content-Type": "application/json; charset=utf-8",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        request_body_bytes=body,
        request_body_sha256=sha256_hex(body),
        wire_sha256="a" * 64,
        marker="<!-- hermes -->",
    )


class AdapterTests(unittest.TestCase):
    def test_github_sends_exact_stored_body_once(self):
        transport = QueueTransport([result(200, {"id": 1}), result(201, {"node_id": "I_1"})])
        adapter = GitHubIssuesAdapter(
            credential_resolver=lambda _p: "token",
            transport=transport,
        )
        item = contract()
        outcome = adapter.dispatch(item)
        self.assertEqual(outcome.disposition, DispatchDisposition.SUCCESS)
        self.assertEqual(transport.calls[-1]["body"], item.request_body_bytes)
        self.assertEqual(sum(call["method"] == "POST" for call in transport.calls), 1)

    def test_pre_send_failure_is_definite_no_effect(self):
        transport = QueueTransport([TransportError("dns", request_may_have_arrived=False)])
        adapter = GitHubIssuesAdapter(credential_resolver=lambda _p: "token", transport=transport)
        self.assertEqual(adapter.dispatch(contract()).disposition, DispatchDisposition.DEFINITE_NO_EFFECT)

    def test_post_send_failure_is_ambiguous(self):
        transport = QueueTransport([
            result(200, {"id": 1}),
            TransportError("timeout", request_may_have_arrived=True),
        ])
        adapter = GitHubIssuesAdapter(credential_resolver=lambda _p: "token", transport=transport)
        self.assertEqual(adapter.dispatch(contract()).disposition, DispatchDisposition.AMBIGUOUS)

    def test_comment_target_rejects_pull_request(self):
        transport = QueueTransport([
            result(200, {"id": 1}),
            result(200, {"number": 7, "pull_request": {"url": "x"}}),
        ])
        adapter = GitHubIssuesAdapter(credential_resolver=lambda _p: "token", transport=transport)
        outcome = adapter.dispatch(contract("github.issue.comment.create", 7))
        self.assertEqual(outcome.disposition, DispatchDisposition.DEFINITE_NO_EFFECT)
        self.assertEqual(outcome.detail_code, "pull_request_comment_forbidden")

    def test_reconciliation_exhaustively_finds_marker(self):
        transport = QueueTransport([
            result(200, {"id": 1}),
            result(200, [{"id": 4, "node_id": "I_4", "number": 4, "body": "x <!-- hermes -->"}]),
        ])
        adapter = GitHubIssuesAdapter(credential_resolver=lambda _p: "token", transport=transport)
        found = adapter.reconcile(contract())
        self.assertTrue(found.complete)
        self.assertEqual(len(found.matches), 1)


if __name__ == "__main__":
    unittest.main()
