"""Compatibility alias for GitHub App authentication."""
from app.github.app_auth import (
    GitHubAppConfig,
    GitHubAppAuthenticator,
    GitHubConfigurationError,
)

__all__ = [
    "GitHubAppConfig",
    "GitHubAppAuthenticator",
    "GitHubConfigurationError",
]
