"""Application factory; deployment wires storage, auth, and Developer B's analyzer here."""
from fastapi import FastAPI
from app.candidates.repository import InMemoryCandidateRepository
from app.candidates.service import CandidateService
from app.github.webhook import create_github_webhook_router

def create_app(webhook_secret: str, candidate_service: CandidateService) -> FastAPI:
    app = FastAPI(title="IssueMatch")
    app.include_router(create_github_webhook_router(webhook_secret, candidate_service))
    return app

# Deliberately not auto-configured from environment: imports/tests do not need secrets.
app = FastAPI(title="IssueMatch")
