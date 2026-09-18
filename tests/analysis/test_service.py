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

class FakeAnalysisProvider:
    def __init__(self, response: str):
        self.response = response
        self.last_prompt = None

    async def generate(self, prompt: str) -> str:
        self.last_prompt = prompt
        return self.response

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
    provider = FakeAnalysisProvider(
        """
        {
            "decision": "PASS",
            "confidence": 0.9,
            "strengths": [
                "The approach identifies the relevant implementation and test files."
            ],
            "issues": [],
            "missing_requirements": [],
            "evidence": [
                "src/auth/session.py exists in the repository context."
            ],
            "revision_feedback": "",
            "recommendation": "The approach is consistent with the repository context."
        }
        """
    )

    service = DefaultAnalysisService(provider)

    candidate = make_candidate(
        "I will modify src/auth/session.py and update tests/test_session.py."
    )

    analysis = await service.analyze(candidate, make_context())

    assert analysis.decision is AnalysisDecision.PASS
    assert analysis.evidence
    assert "src/auth/session.py" in analysis.evidence[0]

@pytest.mark.asyncio
async def test_missing_repository_reference_requires_revision():
    provider = FakeAnalysisProvider(
        """
        {
            "decision": "REVISION_REQUIRED",
            "confidence": 0.4,
            "strengths": [],
            "issues": [
                "The proposed approach does not identify a repository component."
            ],
            "missing_requirements": [
                "Identify the repository component that should be changed."
            ],
            "evidence": [],
            "revision_feedback": "Please identify the relevant repository files or components.",
            "recommendation": ""
        }
        """
    )

    service = DefaultAnalysisService(provider)

    candidate = make_candidate(
        "I will investigate the issue and fix the bug."
    )

    analysis = await service.analyze(candidate, make_context())

    assert analysis.decision is AnalysisDecision.REVISION_REQUIRED
    assert analysis.revision_feedback
    assert provider.last_prompt is not None
    assert "ISSUE" in provider.last_prompt
    assert "CONTRIBUTOR" in provider.last_prompt
    assert "PROPOSED APPROACH" in provider.last_prompt
    assert "RELEVANT REPOSITORY FILES" in provider.last_prompt
    assert "FILE CONTENT" in provider.last_prompt
    assert "TEST FILES" in provider.last_prompt
    assert "MISSING FILES / PATHS" in provider.last_prompt

    assert "src/auth/session.py" in provider.last_prompt
    assert "def timeout(): pass" in provider.last_prompt
    assert "tests/test_session.py" in provider.last_prompt

@pytest.mark.asyncio
async def test_extra_ai_field_is_rejected():
    provider = FakeAnalysisProvider(
        """
        {
            "decision": "PASS",
            "confidence": 0.9,
            "strengths": [],
            "issues": [],
            "missing_requirements": [],
            "evidence": [
                "src/auth/session.py exists in the repository context."
            ],
            "revision_feedback": "",
            "recommendation": "The approach is supported.",
            "unexpected_field": "This must not be accepted."
        }
        """
    )

    service = DefaultAnalysisService(provider)

    candidate = make_candidate(
        "I will modify src/auth/session.py."
    )

    with pytest.raises(Exception):
        await service.analyze(candidate, make_context())

@pytest.mark.asyncio
async def test_pass_without_evidence_is_rejected():
    provider = FakeAnalysisProvider(
        """
        {
            "decision": "PASS",
            "confidence": 0.9,
            "strengths": [
                "The approach looks reasonable."
            ],
            "issues": [],
            "missing_requirements": [],
            "evidence": [],
            "revision_feedback": "",
            "recommendation": "The approach looks good."
        }
        """
    )

    service = DefaultAnalysisService(provider)

    candidate = make_candidate(
        "I will modify src/auth/session.py."
    )

    analysis = await service.analyze(candidate, make_context())

    assert analysis.decision is AnalysisDecision.REVISION_REQUIRED
    assert analysis.revision_feedback
    assert not analysis.evidence