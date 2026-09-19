"""Regression for the public demo issue and the two approach comments."""
import os

import pytest

from app.analysis.provider import AnalysisProviderResult
from app.analysis.service import DefaultAnalysisService
from app.analysis.strands_provider import StrandsAnalysisProvider
from app.candidates.repository import InMemoryCandidateRepository
from app.candidates.service import CandidateService, ProcessingOutcome
from app.models.analysis import AnalysisDecision
from app.models.candidate import CandidateStatus
from tests.fakes import FakeGitHubClient, event


ISSUE_BODY = """Our current Flask backend (app.py) contains these bugs:
- Remove the hardcoded SECRET_KEY and load it from environment variables.
- /api/login must return 401 rather than 200 for invalid credentials.
- /api/users/<user_id> must convert the path parameter to an integer safely.
- Return 404 rather than 500 when a user is not found.

Acceptance Criteria
[ ] Sensitive tokens/secrets are removed from code and loaded via environment variables.
[ ] /api/login returns 401 on invalid credentials.
[ ] /api/users/<user_id> successfully finds and returns user records by ID.
[ ] /api/users/<user_id> returns 404 when a user does not exist.
"""

APP_SOURCE = '''from flask import Flask, request, jsonify
app = Flask(__name__)
SECRET_KEY = "demo-only-token"
users = [{"id": 1, "username": "alice"}]
@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    if data.get('password') == SECRET_KEY:
        return jsonify({"status": "success", "token": SECRET_KEY})
    return jsonify({"status": "error", "message": "Invalid credentials"}), 200
@app.route('/api/users/<user_id>', methods=['GET'])
def get_user(user_id):
    user = next((u for u in users if u["id"] == user_id), None)
    if not user:
        return "User not found", 500
    return jsonify(user)
'''

FIRST_APPROACH = """Hi! I'd love to take this on. Here is my planned approach:
I'll remove the hardcoded SECRET_KEY and use os.getenv() to load credentials from environment variables.
I'll update /api/login so invalid logins return 401 instead of 200.
I'll add int(user_id) conversion with try-except to handle non-integer input.
I'll return 404 instead of 500 when a user is missing.
"""

SECOND_APPROACH = """I'd love to pick this up. My plan is to move the secret token
out of code and fetch it via os.environ; return 401 for failed logins and 404
instead of 500 when a user isn't found; cast the route parameter to an integer
in the user lookup so the stored integer IDs match.
"""

PARTIAL_APPROACH = "I'd like to work on this. I will move SECRET_KEY to os.getenv in app.py, but will not address the login or user lookup bugs yet."
WRONG_APPROACH = "I'd like to work on this. I will only update README.md and leave the Flask backend unchanged."


class DemoClient(FakeGitHubClient):
    def __init__(self):
        super().__init__(files={"README.md": "Demo", "app.py": APP_SOURCE})

    async def get_issue(self, *args):
        return {"title": "Refactor authentication and fix user lookup", "body": ISSUE_BODY, "state": "open"}


class RecordingProvider:
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.last: AnalysisProviderResult | None = None

    async def generate(self, prompt):
        self.last = await self.wrapped.generate(prompt)
        return self.last


@pytest.mark.parametrize("approach", [FIRST_APPROACH, SECOND_APPROACH])
@pytest.mark.skipif(os.getenv("RUN_OLLAMA_TESTS") != "1", reason="Requires local Ollama")
@pytest.mark.asyncio
async def test_each_correct_demo_approach_is_accepted_alone(approach):
    client = DemoClient()
    provider = RecordingProvider(StrandsAnalysisProvider(model_id=os.getenv("OLLAMA_MODEL", "llama3.2:1b")))
    service = CandidateService(InMemoryCandidateRepository(), lambda _: client, DefaultAnalysisService(provider))
    result = await service.process_issue_comment(event("demo-solo", approach))
    assert result.outcome is ProcessingOutcome.ANALYZED, (
        result.detail, provider.last.decision, provider.last.confidence,
        [(item.path, item.excerpt[:40]) for item in provider.last.evidence],
    )
    assert result.analysis.decision is AnalysisDecision.PASS, provider.last
    assert "Status - ACCEPTED" in client.comments[0]


@pytest.mark.parametrize("approach,expected", [
    (PARTIAL_APPROACH, AnalysisDecision.REVISION_REQUIRED),
    (WRONG_APPROACH, AnalysisDecision.REJECT),
])
@pytest.mark.skipif(os.getenv("RUN_OLLAMA_TESTS") != "1", reason="Requires local Ollama")
@pytest.mark.asyncio
async def test_incomplete_or_unrelated_demo_approach_is_not_accepted(approach, expected):
    client = DemoClient()
    provider = RecordingProvider(StrandsAnalysisProvider(model_id=os.getenv("OLLAMA_MODEL", "llama3.2:1b")))
    service = CandidateService(InMemoryCandidateRepository(), lambda _: client, DefaultAnalysisService(provider))
    result = await service.process_issue_comment(event("demo-negative", approach))
    assert result.outcome is ProcessingOutcome.ANALYZED, provider.last
    assert result.analysis.decision is expected, (provider.last, result.analysis)


@pytest.mark.skipif(os.getenv("RUN_OLLAMA_TESTS") != "1", reason="Requires local Ollama")
@pytest.mark.asyncio
async def test_first_correct_demo_approach_is_accepted_and_later_one_waits():
    client = DemoClient()
    provider = RecordingProvider(StrandsAnalysisProvider(model_id=os.getenv("OLLAMA_MODEL", "llama3.2:1b")))
    service = CandidateService(InMemoryCandidateRepository(), lambda _: client, DefaultAnalysisService(provider))

    first_event = event("demo-first", FIRST_APPROACH).model_copy(update={"comment_author": "yashaskn8", "comment_id": 101})
    first = await service.process_issue_comment(first_event)
    assert first.outcome is ProcessingOutcome.ANALYZED, (first.detail, provider.last)
    assert first.analysis is not None
    assert first.analysis.decision is AnalysisDecision.PASS, (
        [(item.path, item.excerpt, item.claim) for item in provider.last.evidence],
        first.analysis.revision_feedback,
    )
    assert first.candidate.status is CandidateStatus.ACCEPTED
    assert len(client.comments) == 1
    assert "Status - ACCEPTED" in client.comments[0]

    second_event = event("demo-second", SECOND_APPROACH).model_copy(update={"comment_author": "koushiksuresh27", "comment_id": 102})
    second = await service.process_issue_comment(second_event)
    assert second.outcome is ProcessingOutcome.WAITING
    assert len(client.comments) == 1
