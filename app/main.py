"""Application factory and application-scoped services."""
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Callable, Any
from fastapi import FastAPI
from app.candidates.repository import InMemoryCandidateRepository
from app.candidates.service import CandidateService
from app.github.webhook import create_github_webhook_router
from app.storage.sqlite import get_default_db_path
from app.storage.delivery_store import (
    DeliveryStore,
    SQLiteDeliveryStore,
    InMemoryDeliveryStore,
)
from app.services.issue_gate import (
    DedupStore,
    SQLiteDedupStore,
    InMemoryDedupStore,
)
from app.services.write_journal import (
    IssueWriteJournal,
    SQLiteIssueWriteJournal,
    InMemoryIssueWriteJournal,
)
from app.services.worker import DurableWebhookWorker
from app.services.pipeline import VerifierPipeline
from app.scout.agent import ScoutAgent
from app.scout.service import ScoutService
from app.infrastructure.llm_router import (
    MultiLLMProvider,
    ProviderConfigurationError,
    create_scout_provider,
    create_verifier_provider,
)
from app.github.app_auth import GitHubAppAuthenticator, GitHubAppConfig
from app.api.verify import router as verify_router
from app.api.scout import router as scout_router


@dataclass
class AppRuntimeConfig:
    db_path: str = field(default_factory=get_default_db_path)
    reservation_ttl_seconds: float = 300.0
    worker_lease_seconds: float = 60.0
    worker_max_attempts: int = 3
    worker_poll_interval: float = 1.0
    auto_start_worker: bool = False

    @classmethod
    def from_environment(cls, auto_start_worker: bool = False) -> "AppRuntimeConfig":
        db_path = os.environ.get("ISSUE_ANALYZER_DB_PATH") or get_default_db_path()
        reservation_ttl = float(os.environ.get("STALE_RESERVATION_SECONDS", "300.0"))
        lease_seconds = float(os.environ.get("WEBHOOK_JOB_LEASE_SECONDS", "60.0"))
        max_attempts = int(os.environ.get("WEBHOOK_MAX_ATTEMPTS", "3"))
        return cls(
            db_path=db_path,
            reservation_ttl_seconds=reservation_ttl,
            worker_lease_seconds=lease_seconds,
            worker_max_attempts=max_attempts,
            auto_start_worker=auto_start_worker,
        )


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


def build_scout_service(
    dedup_store: DedupStore,
    write_journal: IssueWriteJournal,
    client_factory: Callable[[int], Any] | None = None,
) -> ScoutService:
    """Builds a new ScoutService instance bound to the provided application stores."""
    # Scout LLM: Fail closed if explicitly configured with invalid settings
    if os.environ.get("SCOUT_PROVIDER"):
        scout_llm = create_scout_provider()
    else:
        try:
            scout_llm = create_scout_provider()
        except ProviderConfigurationError:
            raise
        except Exception:
            scout_llm = MultiLLMProvider([])

    # Verifier LLM: Fail closed if explicitly configured with invalid settings
    if os.environ.get("VERIFIER_PROVIDER"):
        verifier_llm = create_verifier_provider()
    else:
        try:
            verifier_llm = create_verifier_provider()
        except ProviderConfigurationError:
            raise
        except Exception:
            verifier_llm = MultiLLMProvider([])

    scout_agent = ScoutAgent(llm_provider=scout_llm)
    resolved_client_factory = client_factory or get_lazy_installation_client_factory()

    verifier_pipeline = VerifierPipeline(
        llm_provider=verifier_llm,
        dedup_store=dedup_store,
        write_journal=write_journal,
    )

    return ScoutService(
        scout_agent=scout_agent,
        verifier_pipeline=verifier_pipeline,
        read_client_factory=resolved_client_factory,
        downstream_write_client_factory=resolved_client_factory,
    )


def create_application(
    webhook_secret: str | Callable[[], str] | None = None,
    candidate_service: CandidateService | None = None,
    delivery_store: DeliveryStore | None = None,
    dedup_store: DedupStore | None = None,
    write_journal: IssueWriteJournal | None = None,
    scout_service: ScoutService | None = None,
    config: AppRuntimeConfig | None = None,
    start_worker: bool = False,
) -> FastAPI:
    if config is not None:
        resolved_config = config
        if start_worker:
            resolved_config.auto_start_worker = True
    else:
        resolved_config = AppRuntimeConfig.from_environment(auto_start_worker=start_worker)

    use_sqlite = resolved_config.auto_start_worker or bool(os.environ.get("ISSUE_ANALYZER_DB_PATH"))
    # Application-scoped stores (created per application unless injected)
    app_delivery_store = (
        delivery_store
        if delivery_store is not None
        else (SQLiteDeliveryStore(resolved_config.db_path) if use_sqlite else InMemoryDeliveryStore())
    )
    app_dedup_store = (
        dedup_store
        if dedup_store is not None
        else (
            SQLiteDedupStore(
                resolved_config.db_path,
                reservation_ttl=resolved_config.reservation_ttl_seconds,
            )
            if use_sqlite
            else InMemoryDedupStore(reservation_ttl=resolved_config.reservation_ttl_seconds)
        )
    )
    app_write_journal = (
        write_journal
        if write_journal is not None
        else (SQLiteIssueWriteJournal(resolved_config.db_path) if use_sqlite else InMemoryIssueWriteJournal())
    )

    # Candidate service (default fallback for issue comments)
    if candidate_service is None:
        cand_repo = InMemoryCandidateRepository()
        lazy_client_factory = get_lazy_installation_client_factory()
        candidate_service = CandidateService(
            repository=cand_repo,
            github_client_factory=lazy_client_factory,
            analysis_service=None,
        )

    # Scout service (always wired to THIS application's own dedup store & journal)
    app_scout_service = (
        scout_service
        if scout_service is not None
        else build_scout_service(
            dedup_store=app_dedup_store,
            write_journal=app_write_journal,
        )
    )

    # Worker instance bound to this application
    worker = DurableWebhookWorker(
        delivery_store=app_delivery_store,
        scout_service=app_scout_service,
        candidate_service=candidate_service,
        lease_seconds=resolved_config.worker_lease_seconds,
        poll_interval=resolved_config.worker_poll_interval,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if resolved_config.auto_start_worker:
            await worker.start()
        yield
        if resolved_config.auto_start_worker:
            await worker.stop()

    application = FastAPI(title="IssueMatch", lifespan=lifespan)

    application.state.config = resolved_config
    application.state.delivery_store = app_delivery_store
    application.state.dedup_store = app_dedup_store
    application.state.write_journal = app_write_journal
    application.state.candidate_service = candidate_service
    application.state.scout_service = app_scout_service
    application.state.webhook_worker = worker

    # Resolve webhook secret dynamically from env if not explicitly passed
    secret_resolver = (
        webhook_secret
        if webhook_secret is not None
        else (lambda: os.getenv("GITHUB_WEBHOOK_SECRET", ""))
    )

    webhook_router = create_github_webhook_router(
        webhook_secret=secret_resolver,
        candidate_service=candidate_service,
        delivery_store=app_delivery_store,
        scout_service=app_scout_service,
        max_attempts=resolved_config.worker_max_attempts,
    )
    for route in webhook_router.routes:
        application.router.routes.append(route)

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
app = create_application(start_worker=True)
