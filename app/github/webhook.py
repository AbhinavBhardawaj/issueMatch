"""FastAPI webhook endpoint with raw-body HMAC verification and local delivery idempotency."""
import hashlib
import hmac
import json
import logging
from typing import Callable, Any
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status

from app.candidates.service import CandidateService
from app.github.events import (
    MalformedGitHubEvent,
    normalize_issue_comment_created,
    normalize_push_event,
)
from app.storage.delivery_store import DeliveryStore, DeliveryClaimStatus

logger = logging.getLogger(__name__)


def verify_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    if not signature or not signature.startswith("sha256=") or not secret:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def create_github_webhook_router(
    webhook_secret: str | Callable[[], str],
    candidate_service: CandidateService | None = None,
    delivery_store: DeliveryStore | None = None,
    scout_service: Any = None,
) -> APIRouter:
    router = APIRouter()

    @router.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
    async def github_webhook(
        request: Request, background_tasks: BackgroundTasks
    ) -> dict[str, str]:
        raw_body = await request.body()
        signature = request.headers.get("X-Hub-Signature-256")
        resolved_secret = webhook_secret() if callable(webhook_secret) else webhook_secret
        if not verify_signature(raw_body, signature, resolved_secret):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

        event_name = request.headers.get("X-GitHub-Event")
        delivery_id = request.headers.get("X-GitHub-Delivery")
        if not delivery_id:
            raise HTTPException(status_code=400, detail="Missing GitHub delivery ID")

        # Explicit ping handshake: acknowledge immediately with zero processing
        if event_name == "ping":
            return {"status": "pong"}

        # 1. Parse JSON payload (must not claim delivery if JSON is malformed)
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Malformed JSON payload")

        # 2. Normalize event envelope (must not claim delivery if event structure is malformed)
        push_event = None
        comment_event = None

        if event_name == "push":
            try:
                push_event = normalize_push_event(payload, delivery_id)
            except MalformedGitHubEvent as exc:
                raise HTTPException(status_code=400, detail=str(exc))

            if push_event is None:
                # Deleted branch, non-default branch, or all-zero after SHA
                return {"status": "ignored"}

        elif event_name == "issue_comment":
            if payload.get("action") != "created":
                return {"status": "ignored"}
            try:
                comment_event = normalize_issue_comment_created(payload, delivery_id)
            except MalformedGitHubEvent:
                raise HTTPException(status_code=400, detail="Malformed issue comment event")

        elif event_name in ("issues", "installation"):
            return {"status": "ignored"}

        else:
            return {"status": "ignored"}

        # 3. Local delivery idempotency claim over exact raw body hash
        body_hash = hashlib.sha256(raw_body).hexdigest()
        if delivery_store:
            claim_result = await delivery_store.claim(delivery_id, body_hash)
            if claim_result.status == DeliveryClaimStatus.DUPLICATE:
                return {"status": "duplicate_delivery_ignored"}
            elif claim_result.status == DeliveryClaimStatus.MISMATCHED_PAYLOAD:
                raise HTTPException(
                    status_code=400,
                    detail="Delivery ID payload mismatch: delivery already claimed with different content",
                )

        # 4. Dispatch processing and manage delivery lifecycle
        if push_event:
            active_scout_service = scout_service
            if active_scout_service is None and hasattr(request.app.state, "scout_service"):
                active_scout_service = getattr(request.app.state, "scout_service", None)

            if active_scout_service:
                async def _process_push_wrapped():
                    try:
                        res = await active_scout_service.process_push(push_event)
                        if res and getattr(res, "failures", None):
                            if delivery_store:
                                await delivery_store.fail(delivery_id, body_hash)
                        else:
                            if delivery_store:
                                await delivery_store.complete(delivery_id, body_hash)
                    except Exception:
                        if delivery_store:
                            await delivery_store.fail(delivery_id, body_hash)
                        raise

                background_tasks.add_task(_process_push_wrapped)

            return {"status": "accepted"}

        elif comment_event:
            active_candidate_service = candidate_service
            if active_candidate_service is None and hasattr(request.app.state, "candidate_service"):
                active_candidate_service = getattr(request.app.state, "candidate_service", None)

            if active_candidate_service:
                async def _process_comment_wrapped():
                    try:
                        await active_candidate_service.process_issue_comment(comment_event)
                        if delivery_store:
                            await delivery_store.complete(delivery_id, body_hash)
                    except Exception:
                        if delivery_store:
                            await delivery_store.fail(delivery_id, body_hash)
                        raise

                background_tasks.add_task(_process_comment_wrapped)

            return {"status": "accepted"}

    return router
