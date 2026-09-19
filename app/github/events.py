"""Conversion of untrusted GitHub webhook JSON into internal events."""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError


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


class GitHubIssueAssignmentEvent(BaseModel):
    """Normalized ``issues.assigned``/``issues.unassigned`` event."""

    model_config = ConfigDict(frozen=True)
    delivery_id: str
    action: str
    repository_owner: str
    repository_name: str
    installation_id: int | None = None
    issue_number: int
    issue_title: str = ""
    issue_body: str = ""
    assignee_username: str
    assignee_github_id: int | None = None
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


def normalize_issue_assignment(payload: dict[str, Any], delivery_id: str) -> GitHubIssueAssignmentEvent:
    """Normalize only issue assignment changes used by candidate resumption."""
    if not delivery_id or payload.get("action") not in {"assigned", "unassigned"}:
        raise MalformedGitHubEvent("Only issues.assigned and issues.unassigned are supported")
    try:
        repository, issue, assignee = payload["repository"], payload["issue"], payload["assignee"]
        if not isinstance(assignee, dict) or not assignee.get("login"):
            raise MalformedGitHubEvent("Assignment event has no assignee")
        installation = payload.get("installation") or {}
        return GitHubIssueAssignmentEvent(
            delivery_id=delivery_id, action=payload["action"],
            repository_owner=repository["owner"]["login"], repository_name=repository["name"],
            installation_id=installation.get("id"), issue_number=issue["number"],
            issue_title=issue.get("title") or "", issue_body=issue.get("body") or "",
            assignee_username=assignee["login"], assignee_github_id=assignee.get("id"),
            repository_default_branch=repository.get("default_branch") or "main",
        )
    except (KeyError, TypeError, ValidationError) as exc:
        if isinstance(exc, MalformedGitHubEvent):
            raise
        raise MalformedGitHubEvent("Malformed issue assignment webhook payload") from exc
