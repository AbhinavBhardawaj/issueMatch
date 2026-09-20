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
import logging
import uvicorn
from fastapi import Request

from dotenv import load_dotenv

# Ensure project root is in sys.path and load .env if present
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
load_dotenv()

from app.main import create_application, AppRuntimeConfig

# Configure safe structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("live_webhook_receiver")


def create_live_app():
    config = AppRuntimeConfig.from_environment(auto_start_worker=True)
    webhook_secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not webhook_secret:
        logger.warning(
            "GITHUB_WEBHOOK_SECRET is empty. Incoming webhooks will be rejected with 401 Unauthorized."
        )

    app = create_application(
        webhook_secret=webhook_secret,
        config=config,
        start_worker=True,
    )

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")

    logger.info(f"Starting Live Webhook Receiver on {host}:{port}")
    logger.info("Ready to accept real GitHub App push deliveries.")

    app = create_live_app()
    uvicorn.run(app, host=host, port=port, log_level="info")
