"""Conversion of untrusted GitHub webhook JSON into internal events."""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class MalformedGitHubEvent(ValueError): pass


class GitHubIssueCommentEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    delivery_id: str
    repository_owner: str
    repository_name: str
    installation_id: int | None = None
    issue_number: int
    issue_title: str = ""
    issue_body: str = ""
    comment_id: int
    comment_body: str = ""
    comment_author: str
    comment_author_id: int | None = None
    comment_created_at: datetime | None = None
    repository_default_branch: str = "main"


def normalize_issue_comment_created(payload: dict[str, Any], delivery_id: str) -> GitHubIssueCommentEvent:
    """Validate and normalize only the issue_comment.created shape we process."""
    if not delivery_id:
        raise MalformedGitHubEvent("GitHub delivery ID is required")
    if payload.get("action") != "created":
        raise MalformedGitHubEvent("Only issue_comment.created is supported")
    try:
        repository, issue, comment = payload["repository"], payload["issue"], payload["comment"]
        owner = repository["owner"]["login"]
        commenter = comment["user"]
        installation = payload.get("installation") or {}
        return GitHubIssueCommentEvent(
            delivery_id=delivery_id, repository_owner=owner, repository_name=repository["name"],
            installation_id=installation.get("id"), issue_number=issue["number"],
            issue_title=issue.get("title") or "", issue_body=issue.get("body") or "",
            comment_id=comment["id"], comment_body=comment.get("body") or "",
            comment_author=commenter["login"], comment_author_id=commenter.get("id"),
            comment_created_at=comment.get("created_at"),
            repository_default_branch=repository.get("default_branch") or "main",
        )
    except (KeyError, TypeError, ValidationError) as exc:
        raise MalformedGitHubEvent("Malformed issue_comment webhook payload") from exc
