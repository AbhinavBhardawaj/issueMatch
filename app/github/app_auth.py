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
        private_key_raw = os.getenv("GITHUB_PRIVATE_KEY")
        secret = os.getenv("GITHUB_WEBHOOK_SECRET")
        missing = [name for name, value in (("GITHUB_APP_ID", app_id), ("GITHUB_PRIVATE_KEY", private_key_raw), ("GITHUB_WEBHOOK_SECRET", secret)) if not value]
        if missing:
            raise GitHubConfigurationError(f"Missing required GitHub App configuration: {', '.join(missing)}")

        # Dual-support: inline PEM string or file path
        if "BEGIN" in private_key_raw and "PRIVATE KEY" in private_key_raw:
            # Inline PEM (possibly with literal \n escapes from env var)
            private_key = private_key_raw.replace("\\n", "\n")
        elif os.path.isfile(private_key_raw):
            # File path to PEM
            with open(private_key_raw, "r") as f:
                private_key = f.read()
            if "BEGIN" not in private_key or "PRIVATE KEY" not in private_key:
                raise GitHubConfigurationError(
                    f"GITHUB_PRIVATE_KEY file '{private_key_raw}' does not contain a valid PEM private key"
                )
        else:
            raise GitHubConfigurationError(
                "GITHUB_PRIVATE_KEY is neither a valid inline PEM string nor an existing file path"
            )

        return cls(app_id=app_id, private_key=private_key, webhook_secret=secret)


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
