"""Small, typed GitHub REST client used only by integration/context code."""
from typing import Any, Protocol
import base64

import httpx


class GitHubClientError(RuntimeError): pass
class GitHubNotFoundError(GitHubClientError): pass
class GitHubRateLimitError(GitHubClientError): pass


class GitHubClient(Protocol):
    async def get_issue(self, owner: str, repo: str, number: int) -> dict[str, Any]: ...
    async def get_issues(self, owner: str, repo: str, state: str = "open", per_page: int = 50) -> list[dict[str, Any]]: ...
    async def get_issue_comments(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]: ...
    async def get_repository(self, owner: str, repo: str) -> dict[str, Any]: ...
    async def get_languages(self, owner: str, repo: str) -> dict[str, int]: ...
    async def get_repository_tree(self, owner: str, repo: str, ref: str) -> tuple[list[dict[str, Any]], bool]: ...
    async def get_file_content(self, owner: str, repo: str, path: str, ref: str) -> str: ...
    async def create_issue_comment(self, owner: str, repo: str, number: int, body: str) -> dict[str, Any]: ...
    async def create_issue(self, owner: str, repo: str, title: str, body: str, labels: list[str] | None = None) -> dict[str, Any]: ...
    async def search_issues(self, owner: str, repo: str, query: str) -> list[dict[str, Any]]: ...


class GitHubRestClient:
    """REST adapter with timeouts and normalized failures; it never logs tokens."""
    def __init__(self, token: str, http_client: httpx.AsyncClient | None = None, base_url: str = "https://api.github.com") -> None:
        self._token, self._client, self._base_url = token, http_client, base_url.rstrip("/")

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        try:
            response = await client.request(method, f"{self._base_url}{path}", headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self._token}", "X-GitHub-Api-Version": "2022-11-28"}, **kwargs)
            if response.status_code == 404: raise GitHubNotFoundError("GitHub resource was not found")
            if response.status_code in (403, 429) and response.headers.get("x-ratelimit-remaining") == "0": raise GitHubRateLimitError("GitHub API rate limit exceeded")
            if not response.is_success: raise GitHubClientError(f"GitHub API request failed with status {response.status_code}")
            return response.json()
        except httpx.HTTPError as exc:
            raise GitHubClientError("GitHub API request failed") from exc
        finally:
            if owns_client: await client.aclose()

    async def get_issue(self, owner: str, repo: str, number: int) -> dict[str, Any]: return await self._request("GET", f"/repos/{owner}/{repo}/issues/{number}")
    async def get_issues(self, owner: str, repo: str, state: str = "open", per_page: int = 50) -> list[dict[str, Any]]: return await self._request("GET", f"/repos/{owner}/{repo}/issues", params={"state": state, "per_page": str(per_page)})
    async def get_issue_comments(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]: return await self._request("GET", f"/repos/{owner}/{repo}/issues/{number}/comments")
    async def get_repository(self, owner: str, repo: str) -> dict[str, Any]: return await self._request("GET", f"/repos/{owner}/{repo}")
    async def get_languages(self, owner: str, repo: str) -> dict[str, int]: return await self._request("GET", f"/repos/{owner}/{repo}/languages")
    async def get_repository_tree(self, owner: str, repo: str, ref: str) -> tuple[list[dict[str, Any]], bool]:
        data = await self._request("GET", f"/repos/{owner}/{repo}/git/trees/{ref}", params={"recursive": "1"})
        return list(data.get("tree", [])), bool(data.get("truncated", False))
    async def get_file_content(self, owner: str, repo: str, path: str, ref: str) -> str:
        data = await self._request("GET", f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref})
        if not isinstance(data, dict) or data.get("type") != "file": raise GitHubClientError("GitHub content response was not a file")
        try: return base64.b64decode(data.get("content", "").replace("\n", "")).decode("utf-8", errors="replace")
        except Exception as exc: raise GitHubClientError("GitHub file content was malformed") from exc
    async def create_issue_comment(self, owner: str, repo: str, number: int, body: str) -> dict[str, Any]: return await self._request("POST", f"/repos/{owner}/{repo}/issues/{number}/comments", json={"body": body})
    async def create_issue(self, owner: str, repo: str, title: str, body: str, labels: list[str] | None = None) -> dict[str, Any]: return await self._request("POST", f"/repos/{owner}/{repo}/issues", json={"title": title, "body": body, "labels": labels or []})
    async def search_issues(self, owner: str, repo: str, query: str) -> list[dict[str, Any]]:
        data = await self._request("GET", "/search/issues", params={"q": f"{query} repo:{owner}/{repo}"})
        return data.get("items", [])
