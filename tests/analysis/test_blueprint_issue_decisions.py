"""Regression for the public blueprint-refactor issue's two contributor plans."""
import os

import pytest

from app.analysis.criteria import check_acceptance_criteria
from app.analysis.provider import AnalysisProviderResult, EvidenceCitation
from app.analysis.service import DefaultAnalysisService
from app.analysis.strands_provider import StrandsAnalysisProvider
from app.candidates.repository import InMemoryCandidateRepository
from app.candidates.service import CandidateService, ProcessingOutcome
from app.models.analysis import AnalysisDecision
from tests.fakes import FakeGitHubClient, event


ISSUE_BODY = """Currently, all our routes, mock data, and app configuration live in a single monolithic app.py file.
Modularize with Flask Blueprints: separate the endpoints into logical blueprints, move mock data and shared configuration into separate modules.
Add a Basic Test Suite (pytest): introduce a tests/ directory with Flask's test client.
Add unit/integration tests covering successful login, invalid credentials, and user retrieval (both success and failure cases).

Acceptance Criteria
[ ] Routes are cleanly separated using Flask Blueprints.
[ ] A tests/ folder is established using pytest.
[ ] Basic test coverage exists for the login and user lookup endpoints.
"""

FIRST_APPROACH = """I'd like to work on this issue. I plan to refactor the Flask application into separate modules using Blueprints for authentication and users, move the shared data/configuration out of the main application file, and add pytest tests using Flask's test client.

I'll cover login success/failure and user lookup cases in the tests and make sure the existing endpoints continue working after the refactor.

Please assign this issue to me."""

SECOND_APPROACH = """I'd like to work on this issue. I reviewed the current app.py, and I can implement the refactor while preserving the existing API surface.
The repository currently has the entire Flask application in app.py: it creates app = Flask(__name__), stores the in-memory users list in the same module, defines /api/login, and defines /api/users/<user_id> there as well. There is currently no tests/ package.
My implementation plan is:
Introduce an application factory in app.py. The factory will register the auth and users Blueprints rather than defining all routes directly on the global app.
Move the existing /api/login handler into an auth Blueprint while preserving the /api/login URL.
Move /api/users/<user_id> into a users Blueprint while preserving the existing /api/users/<user_id> URL.
I'll move mock user data into a separate shared module.
Establish a tests/ directory using pytest and Flask's test client.
Add focused tests for successful /api/login using valid credentials; invalid login credentials; successful retrieval of an existing user; failure when requesting a nonexistent user.
The current user_id is a string while stored IDs are integers, so I'll use Flask's int:user_id converter or validated integer conversion.
This satisfies route separation through Flask Blueprints, a pytest-based tests/ suite, and concrete login and user lookup success/failure tests. Please assign this issue to me."""

APP_SOURCE = """from flask import Flask, request, jsonify
app = Flask(__name__)
users = [{"id": 1, "username": "alice"}]
@app.route('/api/login', methods=['POST'])
def login():
    return jsonify({"status": "success"})
@app.route('/api/users/<user_id>', methods=['GET'])
def get_user(user_id):
    user = next((u for u in users if u['id'] == user_id), None)
    return jsonify(user)
"""


class DemoClient(FakeGitHubClient):
    def __init__(self):
        super().__init__(files={"README.md": "Demo", "app.py": APP_SOURCE})

    async def get_issue(self, *args):
        return {"title": "Refactor app.py into a modular blueprint structure and add a basic test suite",
                "body": ISSUE_BODY, "state": "open"}


class RecordingPassProvider:
    def __init__(self):
        self.calls = 0

    async def generate(self, prompt):
        self.calls += 1
        return AnalysisProviderResult(
            decision=AnalysisDecision.PASS, confidence=.8,
            evidence=[EvidenceCitation(path="app.py", excerpt="app = Flask(__name__)",
                                       claim="The current application is defined in app.py")],
        )


def test_first_plan_lacks_explicit_user_lookup_outcomes_but_second_does_not():
    first = check_acceptance_criteria(ISSUE_BODY, FIRST_APPROACH)
    second = check_acceptance_criteria(ISSUE_BODY, SECOND_APPROACH)
    assert first is not None and second is not None
    assert first.detail_gaps == ("Explain tests for both successful and unsuccessful user retrieval.",)
    assert second.detail_gaps == ()
    assert second.missing == ()


@pytest.mark.asyncio
async def test_blueprint_issue_replies_revision_then_accepts_detailed_plan():
    client = DemoClient()
    provider = RecordingPassProvider()
    service = CandidateService(InMemoryCandidateRepository(), lambda _: client, DefaultAnalysisService(provider))
    first_event = event("blueprint-first", FIRST_APPROACH).model_copy(update={
        "comment_author": "koushiksuresh27", "comment_id": 5744563359,
    })
    second_event = event("blueprint-second", SECOND_APPROACH).model_copy(update={
        "comment_author": "yashaskn8", "comment_id": 5744577862,
    })
    first = await service.process_issue_comment(first_event)
    assert first.outcome is ProcessingOutcome.ANALYZED
    assert first.analysis.decision is AnalysisDecision.REVISION_REQUIRED
    assert "successful and unsuccessful user retrieval" in client.comments[0]
    assert provider.calls == 0

    second = await service.process_issue_comment(second_event)
    assert second.outcome is ProcessingOutcome.ANALYZED
    assert second.analysis.decision is AnalysisDecision.PASS
    assert "Status - ACCEPTED" in client.comments[1]
    assert provider.calls == 1


@pytest.mark.skipif(os.getenv("RUN_OLLAMA_TESTS") != "1", reason="Requires local Ollama")
@pytest.mark.asyncio
async def test_detailed_blueprint_plan_with_local_model_is_accepted():
    client = DemoClient()
    provider = StrandsAnalysisProvider(model_id=os.getenv("OLLAMA_MODEL", "llama3.2:1b"))
    service = CandidateService(InMemoryCandidateRepository(), lambda _: client,
                               DefaultAnalysisService(provider))
    detailed = event("blueprint-live", SECOND_APPROACH).model_copy(update={
        "comment_author": "yashaskn8", "comment_id": 5744577862,
    })
    result = await service.process_issue_comment(detailed)
    assert result.outcome is ProcessingOutcome.ANALYZED, result.detail
    assert result.analysis.decision is AnalysisDecision.PASS, result.analysis
    assert "Status - ACCEPTED" in client.comments[0]
