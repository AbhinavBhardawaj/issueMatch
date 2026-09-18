import pytest

from app.analysis.service import DefaultAnalysisService
from app.models.analysis import AnalysisDecision
from app.models.candidate import CandidateSubmission
from app.models.context import (
    CodeContext,
    CodeFile,
    IssueContext,
    RepositoryAnalysisContext,
    RepositoryContext,
)


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
            relevant_files=[
                "src/auth/session.py",
            ],
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


def make_candidate(approach: str) -> CandidateSubmission:
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
        webhook_delivery_id="test-delivery",
    )


@pytest.mark.asyncio
async def test_relevant_repository_approach_passes():
    service = DefaultAnalysisService()

    candidate = make_candidate(
        "I will modify src/auth/session.py and update tests/test_session.py."
    )

    analysis = await service.analyze(candidate, make_context())

    assert analysis.decision is AnalysisDecision.PASS
    assert analysis.evidence
    assert "src/auth/session.py" in analysis.evidence[0]


@pytest.mark.asyncio
async def test_missing_repository_reference_requires_revision():
    service = DefaultAnalysisService()

    candidate = make_candidate(
        "I will investigate the issue and fix the bug."
    )

    analysis = await service.analyze(candidate, make_context())

    assert analysis.decision is AnalysisDecision.REVISION_REQUIRED
    assert analysis.revision_feedback