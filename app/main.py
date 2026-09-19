"""Application factories for test injection and environment-based production wiring."""
import logging
import os

from fastapi import FastAPI
from app.candidates.repository import DynamoDbCandidateRepository, InMemoryCandidateRepository
from app.candidates.service import CandidateService
from app.github.app_auth import GitHubAppAuthenticator, GitHubAppConfig, GitHubConfigurationError
from app.github.webhook import create_github_webhook_router

logger = logging.getLogger(__name__)

def create_app(webhook_secret: str, candidate_service: CandidateService) -> FastAPI:
    app = FastAPI(title="IssueMatch")
    app.include_router(create_github_webhook_router(webhook_secret, candidate_service))
    return app


def create_production_app() -> FastAPI:
    """Compose the real GitHub, storage, and analysis dependencies from the environment.

    Run this factory with ``uvicorn app.main:create_production_app --factory``.
    DynamoDB is the default. ``ISSUEMATCH_STORAGE=memory`` is intentionally only
    available for a single-process local webhook demonstration.
    """
    github_config = GitHubAppConfig.from_environment()
    authenticator = GitHubAppAuthenticator(github_config)
    storage = os.getenv("ISSUEMATCH_STORAGE", "dynamodb").lower()
    if storage == "memory":
        logger.warning("Using non-persistent in-memory candidate storage for local development")
        repository = InMemoryCandidateRepository()
    elif storage == "dynamodb":
        table_name = os.getenv("DYNAMODB_TABLE")
        if not table_name:
            raise GitHubConfigurationError("DYNAMODB_TABLE is required when ISSUEMATCH_STORAGE=dynamodb")
        repository = DynamoDbCandidateRepository(table_name)
    else:
        raise GitHubConfigurationError("ISSUEMATCH_STORAGE must be 'dynamodb' or 'memory'")

    async def github_client_factory(installation_id: int | None):
        if installation_id is None:
            raise GitHubConfigurationError("GitHub webhook event did not include an installation ID")
        return await authenticator.create_installation_client(installation_id)

    # Developer B's factory owns all Strands/Ollama configuration.
    from app.analysis.factory import create_analysis_service
    candidate_service = CandidateService(repository, github_client_factory, create_analysis_service())
    return create_app(github_config.webhook_secret, candidate_service)

# Kept dependency-free for imports and unit tests. Production uses the factory above.
app = FastAPI(title="IssueMatch")
