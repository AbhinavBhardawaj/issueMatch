"""Issue context collection."""
from typing import Any
from app.github.client import GitHubClient
from app.models.context import IssueContext


async def collect_issue_context(client: GitHubClient, owner: str, repository: str, issue_number: int) -> IssueContext:
    data: dict[str, Any] = await client.get_issue(owner, repository, issue_number)
    labels = [label.get("name", "") for label in data.get("labels", []) if isinstance(label, dict) and label.get("name")]
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    return IssueContext(repository_owner=owner, repository_name=repository, issue_number=issue_number,
        title=data.get("title") or "", body=data.get("body") or "", labels=labels,
        state=data.get("state") or "unknown", author=user.get("login"), author_id=user.get("id"), url=data.get("html_url"))
