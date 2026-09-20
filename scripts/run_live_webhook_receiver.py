"""Live Webhook Receiver Harness for Issue Scout & Verifier.

Runs the real FastAPI application on a local port (e.g. 8000) so that a real GitHub App
push webhook can arrive through a tunnel (e.g. ngrok / cloudflare tunnel / reverse proxy).

Safely logs:
- delivery_id
- repository_id
- commit SHA
- durable job state
- finding ID
- gate result
- issue number (if created)

SECURITY INVARIANT:
Never prints:
- webhook secret
- API keys
- GitHub App private keys
- GitHub installation tokens
- Authorization headers
- LLM source prompts
"""
import os
import sys
import sqlite3
import argparse
import logging
import uvicorn
from fastapi import Request, HTTPException
from dotenv import load_dotenv

# Developer-facing bootstrap: load .env if present (environment variables take precedence)
load_dotenv()

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import create_application, AppRuntimeConfig
from app.infrastructure.llm_router import (
    create_scout_provider,
    create_verifier_provider,
    MultiLLMProvider,
    ProviderConfigurationError,
)
from app.verifier.schemas import LLMInvocationError

# Configure safe structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("live_webhook_receiver")


def run_preflight_validation(config: AppRuntimeConfig) -> bool:
    """Validates mandatory configuration before launching server. Exits non-zero on failure."""
    errors = []

    # 1. GitHub App Credentials
    app_id = os.environ.get("GITHUB_APP_ID", "").strip()
    if not app_id:
        errors.append("GITHUB_APP_ID is missing or empty")
    else:
        try:
            int(app_id)
        except ValueError:
            errors.append(f"GITHUB_APP_ID must be a numeric integer, got '{app_id}'")

    priv_key = os.environ.get("GITHUB_PRIVATE_KEY", "").strip()
    if not priv_key:
        errors.append("GITHUB_PRIVATE_KEY is missing or empty")
    elif not ("BEGIN " in priv_key and " PRIVATE KEY" in priv_key) and not os.path.exists(priv_key):
        errors.append("GITHUB_PRIVATE_KEY is neither a valid PEM block nor an existing file path")

    # 2. Webhook Secret
    webhook_secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "").strip()
    if not webhook_secret:
        errors.append("GITHUB_WEBHOOK_SECRET is missing or empty (required for HMAC verification)")

    # 3. Scout LLM Configuration
    scout_desc = "unconfigured"
    try:
        scout_prov = create_scout_provider()
        if isinstance(scout_prov, MultiLLMProvider) and len(scout_prov.providers) == 0:
            errors.append("Scout LLM has no available providers configured")
        else:
            model_info = getattr(scout_prov, "model", getattr(scout_prov, "models", "multi"))
            scout_desc = f"{type(scout_prov).__name__} ({model_info})"
    except (ProviderConfigurationError, LLMInvocationError, Exception) as exc:
        errors.append(f"Scout LLM configuration invalid: {exc}")

    # 4. Verifier LLM Configuration
    verifier_desc = "unconfigured"
    try:
        verifier_prov = create_verifier_provider()
        if isinstance(verifier_prov, MultiLLMProvider) and len(verifier_prov.providers) == 0:
            errors.append("Verifier LLM has no available providers configured")
        else:
            model_info = getattr(verifier_prov, "model", getattr(verifier_prov, "models", "multi"))
            verifier_desc = f"{type(verifier_prov).__name__} ({model_info})"
    except (ProviderConfigurationError, LLMInvocationError, Exception) as exc:
        errors.append(f"Verifier LLM configuration invalid: {exc}")

    # 5. Database Writability
    db_path = config.db_path
    try:
        db_dir = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(db_dir, exist_ok=True)
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS _preflight_test (id INTEGER PRIMARY KEY)")
            conn.execute("DROP TABLE _preflight_test")
    except Exception as exc:
        errors.append(f"Database path '{db_path}' is not writable: {exc}")

    # 6. Worker Configuration
    if config.worker_poll_interval <= 0:
        errors.append(f"worker_poll_interval must be positive, got {config.worker_poll_interval}")
    if config.worker_lease_seconds <= 0:
        errors.append(f"worker_lease_seconds must be positive, got {config.worker_lease_seconds}")
    if config.worker_max_attempts <= 0:
        errors.append(f"worker_max_attempts must be positive, got {config.worker_max_attempts}")

    if errors:
        logger.error("=== PREFLIGHT VALIDATION FAILED ===")
        for err in errors:
            logger.error(f"  [MISSING/INVALID] {err}")
        logger.error("Cannot launch live webhook receiver with invalid configuration.")
        return False

    logger.info("=== PREFLIGHT VALIDATION PASSED ===")
    logger.info(f"  Scout LLM:               {scout_desc}")
    logger.info(f"  Verifier LLM:            {verifier_desc}")
    logger.info(f"  Durable SQLite:          {db_path}")
    logger.info(f"  Webhook Secret:          configured (HMAC active)")
    logger.info(f"  GitHub App Credentials:  configured (App ID {app_id})")
    logger.info(f"  Worker Lease / Attempts: {config.worker_lease_seconds}s / {config.worker_max_attempts} max")
    logger.info("Prerequisite: Push must target sandbox repository's default branch.")
    return True


def create_live_app():
    config = AppRuntimeConfig.from_environment(auto_start_worker=True)

    if not run_preflight_validation(config):
        sys.exit(1)

    webhook_secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")

    app = create_application(
        webhook_secret=webhook_secret,
        config=config,
        start_worker=True,
    )

    # Diagnostic endpoint to safely inspect delivery job state without secrets
    @app.get("/admin/jobs/{delivery_id}")
    async def inspect_job_state(delivery_id: str):
        delivery_store = getattr(app.state, "delivery_store", None)
        if not delivery_store or not hasattr(delivery_store, "get_job"):
            raise HTTPException(status_code=501, detail="Durable delivery store not active")
        job = await delivery_store.get_job(delivery_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Delivery {delivery_id} not found")
        return {
            "delivery_id": job.delivery_id,
            "status": job.status.value if hasattr(job.status, "value") else str(job.status),
            "event_type": job.event_type,
            "attempts": job.attempts,
            "max_attempts": job.max_attempts,
            "last_error": job.last_error,
            "created_at": str(job.created_at) if hasattr(job, "created_at") else None,
            "updated_at": str(job.updated_at) if hasattr(job, "updated_at") else None,
        }

    # Middleware to log delivery arrival safely
    @app.middleware("http")
    async def safe_delivery_logger(request: Request, call_next):
        if request.url.path == "/webhooks/github":
            delivery_id = request.headers.get("X-GitHub-Delivery", "unknown")
            event_type = request.headers.get("X-GitHub-Event", "unknown")
            logger.info(
                f"[WEBHOOK ARRIVED] Event: {event_type} | Delivery ID: {delivery_id}"
            )
        response = await call_next(request)
        return response

    return app


def inspect_delivery_cli(db_path: str, delivery_id: str):
    """Directly inspects the SQLite database for a delivery ID without launching server."""
    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}")
        sys.exit(1)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT delivery_id, event_type, status, attempts, max_attempts, last_error, created_at, updated_at "
            "FROM webhook_jobs WHERE delivery_id = ?",
            (delivery_id,),
        )
        row = cur.fetchone()
        if not row:
            print(f"No delivery job found for delivery_id: {delivery_id}")
            sys.exit(1)
        print("=== DELIVERY JOB DETAILS ===")
        for k in row.keys():
            print(f"  {k}: {row[k]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Live Webhook Receiver Harness")
    parser.add_argument("--inspect", type=str, help="Inspect a specific delivery_id in SQLite DB and exit")
    args = parser.parse_args()

    default_db = os.environ.get("ISSUE_ANALYZER_DB_PATH", "issueanalyzer_durable.db")

    if args.inspect:
        inspect_delivery_cli(default_db, args.inspect)
        sys.exit(0)

    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")

    logger.info(f"Initializing Live Webhook Receiver on {host}:{port}")
    app = create_live_app()
    logger.info(f"Starting server on {host}:{port} — Ready to accept real GitHub App push deliveries.")
    uvicorn.run(app, host=host, port=port, log_level="info")
