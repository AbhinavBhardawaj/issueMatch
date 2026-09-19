import os
import pytest
pytest.importorskip("strands")

from app.analysis.service import DefaultAnalysisService
from app.analysis.strands_provider import StrandsAnalysisProvider
from app.models.analysis import AnalysisDecision
from app.models.candidate import CandidateSubmission
from app.models.context import (
    CodeContext,
    CodeFile,
    IssueContext,
    RepositoryAnalysisContext,
    RepositoryContext,
)


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