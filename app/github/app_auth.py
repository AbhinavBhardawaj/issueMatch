"""GitHub App JWT and installation-token authentication."""
from dataclasses import dataclass
import os
import time

import httpx
import jwt


class GitHubConfigurationError(RuntimeError):
    """Raised when required GitHub App configuration is absent or invalid."""


@dataclass(frozen=True)
class GitHubAppConfig:
    app_id: str
    private_key: str
    webhook_secret: str

    @classmethod
    def from_environment(cls) -> "GitHubAppConfig":
        app_id = os.getenv("GITHUB_APP_ID")
        private_key = os.getenv("GITHUB_PRIVATE_KEY")
        secret = os.getenv("GITHUB_WEBHOOK_SECRET")
        missing = [name for name, value in (("GITHUB_APP_ID", app_id), ("GITHUB_PRIVATE_KEY", private_key), ("GITHUB_WEBHOOK_SECRET", secret)) if not value]
        if missing:
            raise GitHubConfigurationError(f"Missing required GitHub App configuration: {', '.join(missing)}")
        return cls(app_id=app_id, private_key=private_key.replace("\\n", "\n"), webhook_secret=secret)


class GitHubAppAuthenticator:
    """Creates short-lived GitHub App JWTs and installation access tokens."""

    api_base_url = "https://api.github.com"

    def __init__(self, config: GitHubAppConfig, http_client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._http_client = http_client

    @property
    def webhook_secret(self) -> str:
        return self._config.webhook_secret

    def create_app_jwt(self, now: int | None = None) -> str:
        issued_at = now or int(time.time())
        return jwt.encode({"iat": issued_at - 60, "exp": issued_at + 540, "iss": self._config.app_id}, self._config.private_key, algorithm="RS256")

    async def get_installation_token(self, installation_id: int) -> str:
        if installation_id <= 0:
            raise GitHubConfigurationError("A positive GitHub installation ID is required")
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self.create_app_jwt()}", "X-GitHub-Api-Version": "2022-11-28"}
        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=10.0)
        try:
            response = await client.post(f"{self.api_base_url}/app/installations/{installation_id}/access_tokens", headers=headers)
            response.raise_for_status()
            token = response.json().get("token")
            if not isinstance(token, str) or not token:
                raise GitHubConfigurationError("GitHub installation token response was malformed")
            return token
        finally:
            if owns_client:
                await client.aclose()

    async def create_installation_client(self, installation_id: int):
        """Create the repository-scoped REST client used by webhook processing."""
        from app.github.client import GitHubRestClient
        return GitHubRestClient(await self.get_installation_token(installation_id))
