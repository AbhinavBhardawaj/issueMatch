"""FastAPI webhook endpoint with raw-body HMAC verification."""
import hashlib
import hmac
import json
import logging
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status

from app.candidates.service import CandidateService
from app.github.events import MalformedGitHubEvent, normalize_issue_comment_created

logger = logging.getLogger(__name__)

def verify_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    if not signature or not signature.startswith("sha256=") or not secret: return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)

def create_github_webhook_router(webhook_secret: str, candidate_service: CandidateService) -> APIRouter:
    router = APIRouter()
    @router.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
    async def github_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, str]:
        raw_body = await request.body()
        signature = request.headers.get("X-Hub-Signature-256")
        if not verify_signature(raw_body, signature, webhook_secret):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
        event_name, delivery_id = request.headers.get("X-GitHub-Event"), request.headers.get("X-GitHub-Delivery")
        if not delivery_id: raise HTTPException(status_code=400, detail="Missing GitHub delivery ID")
        if event_name not in {"issues", "issue_comment"}: return {"status": "ignored"}
        try: payload = json.loads(raw_body)
        except json.JSONDecodeError: raise HTTPException(status_code=400, detail="Malformed JSON payload")
        if event_name == "issues": return {"status": "ignored"}
        if payload.get("action") != "created": return {"status": "ignored"}
        try: normalized = normalize_issue_comment_created(payload, delivery_id)
        except MalformedGitHubEvent: raise HTTPException(status_code=400, detail="Malformed issue comment event")
        background_tasks.add_task(candidate_service.process_issue_comment, normalized)
        return {"status": "accepted"}
    return router
