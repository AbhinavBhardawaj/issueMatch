from typing import Protocol, Any


class ScoutGitHubReadClient(Protocol):
    """
    Read-only GitHub client protocol specifically for Issue Scout.
    Exposes zero write methods (no create_issue, create_issue_comment, etc.).
    """

    async def get_repository(self, owner: str, repo: str) -> dict[str, Any]: ...
    async def compare_commits(
        self, owner: str, repo: str, before_sha: str, after_sha: str
    ) -> dict[str, Any]: ...
    async def get_repository_tree(
        self, owner: str, repo: str, ref: str
    ) -> tuple[list[dict[str, Any]], bool]: ...
    async def get_file_content(
        self, owner: str, repo: str, path: str, ref: str
    ) -> str: ...
    async def get_issues(
        self, owner: str, repo: str, state: str = "open", per_page: int = 50
    ) -> list[dict[str, Any]]: ...


class SecurityIdentityError(RuntimeError):
    """Raised when repository identity does not match GitHub metadata (fail closed)."""
    pass


async def verify_repository_identity(
    client: ScoutGitHubReadClient,
    owner: str,
    repo_name: str,
    expected_repo_id: int,
) -> dict[str, Any]:
    """
    Verify returned numeric repository ID matches push_event.repository_id.
    Fails closed if there is any mismatch.
    """
    repo_data = await client.get_repository(owner, repo_name)
    actual_id = int(repo_data.get("id", 0))
    if actual_id != expected_repo_id:
        raise SecurityIdentityError(
            f"Repository ID mismatch: expected {expected_repo_id}, got {actual_id} from GitHub"
        )
    return repo_data
