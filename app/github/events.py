import re
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


class GitHubPushEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    delivery_id: str
    installation_id: int
    repository_id: int

    repository_owner: str
    repository_name: str
    default_branch: str

    before_sha: str
    after_sha: str
    ref: str

    forced: bool = False
    deleted: bool = False


SHA_REGEX = re.compile(r"^[0-9a-fA-F]{40}$")
ALL_ZERO_SHA = "0" * 40


def normalize_push_event(payload: dict[str, Any], delivery_id: str) -> GitHubPushEvent | None:
    """
    Deterministically validate and normalize GitHub push webhook payloads.
    Returns GitHubPushEvent if eligible for Scout analysis (pushes targeting refs/heads/<default_branch>).
    Returns None if safely ignored (branch deletion, all-zero after SHA, non-default branch).
    Raises MalformedGitHubEvent on syntactically invalid or untrusted payloads.
    """
    if not delivery_id or not delivery_id.strip():
        raise MalformedGitHubEvent("Missing or empty GitHub delivery ID")

    if not isinstance(payload, dict):
        raise MalformedGitHubEvent("Payload must be a dictionary")

    try:
        ref = payload.get("ref")
        if not ref or not isinstance(ref, str) or not ref.strip():
            raise MalformedGitHubEvent("Missing required push field: ref")
        installation = payload.get("installation")
        if not installation or not isinstance(installation, dict):
            raise MalformedGitHubEvent("Missing installation information")
        installation_id = installation.get("id")
        if not isinstance(installation_id, int) or installation_id <= 0:
            raise MalformedGitHubEvent("Installation ID must be a positive integer")

        repository = payload.get("repository")
        if not repository or not isinstance(repository, dict):
            raise MalformedGitHubEvent("Missing repository information")

        repository_id = repository.get("id")
        if not isinstance(repository_id, int) or repository_id <= 0:
            raise MalformedGitHubEvent("Repository ID must be a positive integer")

        owner_dict = repository.get("owner")
        if not isinstance(owner_dict, dict):
            raise MalformedGitHubEvent("Missing repository owner")
        owner = owner_dict.get("login") or owner_dict.get("name")
        if not owner or not isinstance(owner, str) or not owner.strip():
            raise MalformedGitHubEvent("Repository owner must be non-empty string")

        repo_name = repository.get("name")
        if not repo_name or not isinstance(repo_name, str) or not repo_name.strip():
            raise MalformedGitHubEvent("Repository name must be non-empty string")

        default_branch = repository.get("default_branch")
        if not default_branch or not isinstance(default_branch, str) or not default_branch.strip():
            default_branch = "main"

        before_sha = payload.get("before")
        after_sha = payload.get("after")

        if not before_sha or not isinstance(before_sha, str) or not SHA_REGEX.match(before_sha):
            raise MalformedGitHubEvent(f"Invalid before SHA: {before_sha}")

        if not after_sha or not isinstance(after_sha, str) or not SHA_REGEX.match(after_sha):
            raise MalformedGitHubEvent(f"Invalid after SHA: {after_sha}")

        deleted = bool(payload.get("deleted", False))
        forced = bool(payload.get("forced", False))

        # Check for branch deletion or all-zero after SHA
        if deleted or after_sha == ALL_ZERO_SHA:
            return None

        # MVP constraint: only analyze pushes targeting refs/heads/<default_branch>
        expected_ref = f"refs/heads/{default_branch}"
        if ref != expected_ref:
            return None

        return GitHubPushEvent(
            delivery_id=delivery_id.strip(),
            installation_id=installation_id,
            repository_id=repository_id,
            repository_owner=owner.strip(),
            repository_name=repo_name.strip(),
            default_branch=default_branch.strip(),
            before_sha=before_sha,
            after_sha=after_sha,
            ref=ref.strip(),
            forced=forced,
            deleted=deleted,
        )
    except MalformedGitHubEvent:
        raise
    except Exception as exc:
        raise MalformedGitHubEvent(f"Malformed push event: {str(exc)}") from exc


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
