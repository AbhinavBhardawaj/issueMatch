"""Application factory and application-scoped services."""
import os
from typing import Callable, Any
from fastapi import FastAPI
from app.candidates.repository import InMemoryCandidateRepository
from app.candidates.service import CandidateService
from app.github.webhook import create_github_webhook_router
from app.storage.delivery_store import InMemoryDeliveryStore, DeliveryStore
from app.services.issue_gate import InMemoryDedupStore
from app.services.pipeline import VerifierPipeline
from app.scout.agent import ScoutAgent
from app.scout.service import ScoutService
from app.infrastructure.llm_router import MultiLLMProvider, load_providers
from app.github.app_auth import GitHubAppAuthenticator, GitHubAppConfig
from app.api.verify import router as verify_router
from app.api.scout import router as scout_router

# Application-scoped instances (reused across requests, not recreated)
_delivery_store = InMemoryDeliveryStore()
_dedup_store = InMemoryDedupStore()
_scout_service: ScoutService | None = None


def get_application_delivery_store() -> InMemoryDeliveryStore:
    return _delivery_store


def get_application_dedup_store() -> InMemoryDedupStore:
    return _dedup_store


def get_lazy_installation_client_factory() -> Callable[[int], Any]:
    """
    Returns a factory that creates GitHub installation clients on demand.
    Resolves GitHubAppConfig lazily so module import does not require credentials.
    Fails closed if configuration is missing when an authenticated call is made.
    """
    async def installation_client_factory(installation_id: int):
        config = GitHubAppConfig.from_environment()
        authenticator = GitHubAppAuthenticator(config)
        return await authenticator.create_installation_client(installation_id)

    return installation_client_factory


def get_application_scout_service(dedup_store: InMemoryDedupStore | None = None) -> ScoutService:
    global _scout_service
    if _scout_service is None:
        try:
            providers = load_providers()
        except Exception:
            providers = []
        multi_llm = MultiLLMProvider(providers)
        scout_agent = ScoutAgent(llm_provider=multi_llm)

        client_factory = get_lazy_installation_client_factory()

        verifier_pipeline = VerifierPipeline(
            llm_provider=multi_llm,
            dedup_store=dedup_store or _dedup_store,
        )

        _scout_service = ScoutService(
            scout_agent=scout_agent,
            verifier_pipeline=verifier_pipeline,
            read_client_factory=client_factory,
            downstream_write_client_factory=client_factory,
        )
    return _scout_service


def create_application(
    webhook_secret: str | Callable[[], str] | None = None,
    candidate_service: CandidateService | None = None,
    delivery_store: DeliveryStore | None = None,
    dedup_store: InMemoryDedupStore | None = None,
    scout_service: ScoutService | None = None,
) -> FastAPI:
    application = FastAPI(title="IssueMatch")

    # Application-scoped stores (created per application unless injected)
    app_delivery_store = delivery_store if delivery_store is not None else InMemoryDeliveryStore()
    app_dedup_store = dedup_store if dedup_store is not None else InMemoryDedupStore()

    application.state.delivery_store = app_delivery_store
    application.state.dedup_store = app_dedup_store

    # Candidate service (default fallback for issue comments)
    if candidate_service is None:
        cand_repo = InMemoryCandidateRepository()
        lazy_client_factory = get_lazy_installation_client_factory()
        candidate_service = CandidateService(
            repository=cand_repo,
            github_client_factory=lazy_client_factory,
            analysis_service=None,
        )
    application.state.candidate_service = candidate_service

    # Scout service
    app_scout_service = scout_service if scout_service is not None else get_application_scout_service()
    application.state.scout_service = app_scout_service

    # Resolve webhook secret dynamically from env if not explicitly passed
    secret_resolver = webhook_secret if webhook_secret is not None else (lambda: os.getenv("GITHUB_WEBHOOK_SECRET", ""))

    application.include_router(
        create_github_webhook_router(
            webhook_secret=secret_resolver,
            candidate_service=candidate_service,
            delivery_store=app_delivery_store,
            scout_service=app_scout_service,
        )
    )

    # Conditionally mount dev endpoints
    if os.getenv("ENABLE_DEV_VERIFY_API", "").lower() in ("true", "1", "yes"):
        application.include_router(verify_router, prefix="/api")

    if os.getenv("ENABLE_DEV_SCOUT_API", "").lower() in ("true", "1", "yes"):
        application.include_router(scout_router, prefix="/api")

    return application


def create_app(
    webhook_secret: str | Callable[[], str] | None = None,
    candidate_service: CandidateService | None = None,
    delivery_store: DeliveryStore | None = None,
    scout_service: ScoutService | None = None,
) -> FastAPI:
    return create_application(
        webhook_secret=webhook_secret,
        candidate_service=candidate_service,
        delivery_store=delivery_store,
        scout_service=scout_service,
    )


# Default instance for ASGI servers (uvicorn app.main:app)
app = create_application()
