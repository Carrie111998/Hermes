from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass


class GitHubError(RuntimeError):
    pass


def _parse_link_header(value: str) -> dict[str, str]:
    links = {}
    for part in value.split(","):
        match = re.match(r'\s*<([^>]+)>;\s*rel="([^"]+)"', part)
        if match:
            links[match.group(2)] = match.group(1)
    return links


def resolve_token() -> str | None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return result.stdout.strip() or None
    return None


@dataclass
class GitHubClient:
    token: str | None = None
    base_url: str = "https://api.github.com"

    def __post_init__(self) -> None:
        if self.token is None:
            self.token = resolve_token()

    def _get(self, url: str) -> tuple[object, str]:
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read()), resp.headers.get("Link", "")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None, ""
            body = exc.read().decode("utf-8", errors="replace")
            raise GitHubError(f"GET {url} -> {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise GitHubError(f"GET {url} failed: {exc.reason}") from exc

    def _request(self, path: str) -> object:
        body, _ = self._get(f"{self.base_url}{path}")
        return body

    def get_issue(self, owner: str, repo: str, number: int) -> dict | None:
        return self._request(f"/repos/{owner}/{repo}/issues/{number}")

    def get_issue_timeline(self, owner: str, repo: str, number: int) -> list[dict]:
        result = self._request(f"/repos/{owner}/{repo}/issues/{number}/timeline")
        return result or []

    def get_repo_file_text(self, owner: str, repo: str, path: str) -> str | None:
        result = self._request(f"/repos/{owner}/{repo}/contents/{path}")
        if not result or "content" not in result:
            return None
        return base64.b64decode(result["content"]).decode("utf-8", errors="replace")

    def _paginated(self, path: str, params: dict | None = None) -> list[dict]:
        # Page-number pagination (page=N) is rejected past 10,000 items on
        # large repos ("use cursor based pagination"). Following the Link
        # header's next URL is what GitHub actually asks for and has no
        # such ceiling.
        query_params = dict(params or {}, per_page="100")
        query = "&".join(f"{k}={v}" for k, v in query_params.items())
        url: str | None = f"{self.base_url}{path}?{query}"
        items: list[dict] = []
        while url:
            batch, link_header = self._get(url)
            if not batch:
                break
            items.extend(batch)
            url = _parse_link_header(link_header).get("next")
        return items

    def list_org_repos(self, org: str) -> list[dict]:
        return self._paginated(f"/orgs/{org}/repos", {"type": "public"})

    def list_repo_issues(self, owner: str, repo: str, state: str = "all") -> list[dict]:
        return self._paginated(f"/repos/{owner}/{repo}/issues", {"state": state})

    def list_repo_issue_comments(self, owner: str, repo: str) -> list[dict]:
        return self._paginated(
            f"/repos/{owner}/{repo}/issues/comments", {"sort": "created", "direction": "asc"}
        )
