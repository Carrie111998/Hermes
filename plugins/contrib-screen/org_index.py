from __future__ import annotations

from .index_store import IndexStore


def _comment_issue_number(issue_url: str) -> str:
    return issue_url.rstrip("/").rsplit("/", 1)[-1]


def sync_repo(store: IndexStore, client, owner: str, repo: str) -> dict:
    full_name = f"{owner}/{repo}"
    issues = client.list_repo_issues(owner, repo)
    for issue in issues:
        store.upsert_issue(
            full_name,
            str(issue["number"]),
            is_pr="pull_request" in issue,
            state=issue.get("state"),
            title=issue.get("title"),
            body=issue.get("body"),
            author=(issue.get("user") or {}).get("login"),
            assignees=[a["login"] for a in (issue.get("assignees") or [])],
            created_at=issue.get("created_at"),
            updated_at=issue.get("updated_at"),
            closed_at=issue.get("closed_at"),
            url=issue.get("html_url"),
        )

    comments = client.list_repo_issue_comments(owner, repo)
    store.add_comments([
        {
            "repo": full_name,
            "number": _comment_issue_number(c["issue_url"]),
            "comment_id": c["id"],
            "author": (c.get("user") or {}).get("login"),
            "body": c.get("body"),
            "created_at": c.get("created_at"),
            "url": c.get("html_url"),
        }
        for c in comments
    ])

    store.upsert_repo(full_name)
    store.commit()
    return {"repo": full_name, "issues": len(issues), "comments": len(comments)}


def sync_org(store: IndexStore, client, org: str, repos: list[str] | None = None) -> list[dict]:
    targets = repos if repos is not None else [r["name"] for r in client.list_org_repos(org)]
    return [sync_repo(store, client, org, repo) for repo in targets]
