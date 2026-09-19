import os
import pytest
pytest.importorskip("strands")

from app.analysis.service import DefaultAnalysisService
from app.analysis.strands_provider import StrandsAnalysisProvider
from app.candidates.repository import InMemoryCandidateRepository
from app.candidates.service import CandidateService, ProcessingOutcome
from app.models.analysis import AnalysisDecision
from app.models.candidate import CandidateSubmission
from app.models.context import (
    CodeContext,
    CodeFile,
    IssueContext,
    RepositoryAnalysisContext,
    RepositoryContext,
)
from tests.fakes import FakeGitHubClient, event


pytestmark = pytest.mark.asyncio


def make_context() -> RepositoryAnalysisContext:
    return RepositoryAnalysisContext(
        issue_context=IssueContext(
            repository_owner="acme",
            repository_name="demo",
            issue_number=7,
            title="Session timeout",
            body="Fix session timeout handling.",
        ),
        repository_context=RepositoryContext(
            owner="acme",
            name="demo",
        ),
        code_context=CodeContext(
            relevant_files=["src/auth/session.py"],
            file_contents=[
                CodeFile(
                    path="src/auth/session.py",
                    content="def timeout(): pass",
                ),
            ],
            test_files=[
                CodeFile(
                    path="tests/test_session.py",
                    content="def test_timeout(): pass",
                ),
            ],
        ),
    )


def make_candidate() -> CandidateSubmission:
    approach = (
        "I will modify src/auth/session.py to fix the timeout handling "
        "and update tests/test_session.py with a regression test."
    )

    return CandidateSubmission(
        repository_owner="acme",
        repository_name="demo",
        issue_number=7,
        issue_title="Session timeout",
        issue_body="Fix session timeout handling.",
        contributor_username="alice",
        comment_id=9,
        comment_body=approach,
        approach=approach,
        webhook_delivery_id="ollama-test",
    )


@pytest.mark.skipif(
    os.getenv("RUN_OLLAMA_TESTS") != "1",
    reason="Set RUN_OLLAMA_TESTS=1 to run the real Ollama integration test.",
)
async def test_real_ollama_analysis():
    provider = StrandsAnalysisProvider(
        host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
        model_id=os.getenv("OLLAMA_MODEL", "llama3.2:1b"),
    )

    service = DefaultAnalysisService(provider)

    analysis = await service.analyze(
        make_candidate(),
        make_context(),
    )

    assert analysis.decision in {
        AnalysisDecision.PASS,
        AnalysisDecision.REVISION_REQUIRED,
        AnalysisDecision.REJECT,
    }
    assert 0 <= analysis.confidence <= 1

    if analysis.decision is AnalysisDecision.PASS:
        assert analysis.evidence


@pytest.mark.skipif(
    os.getenv("RUN_OLLAMA_TESTS") != "1",
    reason="Set RUN_OLLAMA_TESTS=1 to run the real Ollama integration test.",
)
async def test_real_ollama_candidate_pipeline_posts_comment():
    source = '''from flask import Flask, request, jsonify
app = Flask(__name__)
SECRET_KEY = "example-demo-token"
users = [{"id": 1, "username": "alice"}]
@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    if data.get('password') == SECRET_KEY:
        return jsonify({"status": "success"})
    return jsonify({"status": "error"}), 200
@app.route('/api/users/<user_id>', methods=['GET'])
def get_user(user_id):
    user = next((u for u in users if u["id"] == user_id), None)
    if not user:
        return "User not found", 500
    return jsonify(user)
'''

    class DemoClient(FakeGitHubClient):
        async def get_issue(self, *args):
            return {
                "title": "Fix authentication and user lookup",
                "body": "Fix app.py: move hardcoded secret to an environment variable, return 401 for bad login, cast user_id safely, and return 404 for missing users.",
                "state": "open",
            }

    client = DemoClient(files={"README.md": "Demo", "app.py": source})
    analysis_service = DefaultAnalysisService(StrandsAnalysisProvider(
        host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
        model_id=os.getenv("OLLAMA_MODEL", "llama3.2:1b"),
    ))
    service = CandidateService(InMemoryCandidateRepository(), lambda _: client, analysis_service)
    comment = (
        "I'd love to take this on. I'll use os.getenv for the secret, make failed /api/login return 401, "
        "cast user_id to int with error handling, and return 404 for missing users."
    )
    result = await service.process_issue_comment(event("ollama-demo", comment))
    assert result.outcome is ProcessingOutcome.ANALYZED
    assert result.analysis is not None
    assert result.analysis.decision in AnalysisDecision
    assert len(client.comments) == 1
    assert "Status - " in client.comments[0]
