import pytest

from app.analysis.criteria import check_acceptance_criteria
from app.analysis.provider import AnalysisProviderResult, EvidenceCitation
from app.analysis.service import DefaultAnalysisService
from app.models.analysis import AnalysisDecision
from app.models.candidate import CandidateSubmission
from app.models.context import CodeContext, CodeFile, IssueContext, RepositoryAnalysisContext, RepositoryContext
from tests.analysis.test_demo_issue_decisions import (
    APP_SOURCE, FIRST_APPROACH, ISSUE_BODY, PARTIAL_APPROACH, SECOND_APPROACH, WRONG_APPROACH,
)


@pytest.mark.parametrize("approach,count", [
    (FIRST_APPROACH, 4), (SECOND_APPROACH, 4),
    (PARTIAL_APPROACH, 1), (WRONG_APPROACH, 0),
])
def test_explicit_acceptance_criteria_coverage(approach, count):
    coverage = check_acceptance_criteria(ISSUE_BODY, approach)
    assert coverage is not None
    assert len(coverage.covered) == count


def test_unstructured_issue_does_not_trigger_lexical_gate():
    assert check_acceptance_criteria("Please fix the bug", FIRST_APPROACH) is None


def test_negated_keywords_do_not_count_as_implemented_requirements():
    approach = ("I will move SECRET_KEY to the environment, but will not update "
                "/api/login to 401; I will not cast user_id to an integer; "
                "I will not return 404 for missing users.")
    coverage = check_acceptance_criteria(ISSUE_BODY, approach)
    assert coverage is not None
    assert len(coverage.covered) == 1


class PassWithoutCitations:
    async def generate(self, prompt):
        return AnalysisProviderResult(decision=AnalysisDecision.PASS, confidence=.8, evidence=[])


class ZeroConfidenceRevision:
    async def generate(self, prompt):
        return AnalysisProviderResult(decision=AnalysisDecision.REVISION_REQUIRED,
                                      confidence=0, evidence=[],
                                      revision_feedback="Please clarify the implementation and tests.")


class ConfidentConcreteRevision:
    async def generate(self, prompt):
        return AnalysisProviderResult(decision=AnalysisDecision.REVISION_REQUIRED,
                                      confidence=.7, evidence=[],
                                      issues=["The proposed change would expose the secret in a response."],
                                      revision_feedback="Do not return the secret in the login response.")


class RejectionWithoutReason:
    async def generate(self, prompt):
        return AnalysisProviderResult(decision=AnalysisDecision.REJECT, confidence=.6,
                                      evidence=[], issues=[], recommendation="")


class ZeroConfidenceInventedPath:
    async def generate(self, prompt):
        return AnalysisProviderResult(decision=AnalysisDecision.PASS, confidence=0,
                                      evidence=[EvidenceCitation(path="nonexistent.py", excerpt="def fix():",
                                                                 claim="The fix belongs here")])


class InvalidExcerptInRealFile:
    async def generate(self, prompt):
        return AnalysisProviderResult(decision=AnalysisDecision.PASS, confidence=1,
                                      evidence=[EvidenceCitation(path="app.py", excerpt="made up source line",
                                                                 claim="The fix belongs here")])


class MixedRealAndUnseenCitations:
    async def generate(self, prompt):
        return AnalysisProviderResult(decision=AnalysisDecision.PASS, confidence=1,
                                      evidence=[EvidenceCitation(path="app.py", excerpt="def login():",
                                                                 claim="Login logic is here"),
                                                EvidenceCitation(path="README.md", excerpt="invented text",
                                                                 claim="README explains the fix")])


@pytest.mark.asyncio
@pytest.mark.parametrize("approach,expected", [
    (FIRST_APPROACH, AnalysisDecision.PASS),
    (SECOND_APPROACH, AnalysisDecision.PASS),
    (PARTIAL_APPROACH, AnalysisDecision.REVISION_REQUIRED),
    (WRONG_APPROACH, AnalysisDecision.REJECT),
])
async def test_checklist_prevents_false_acceptance(approach, expected):
    candidate = CandidateSubmission(repository_owner="atul812", repository_name="demo_repo", issue_number=2,
                                    contributor_username="contributor", comment_id=1, comment_body=approach,
                                    approach=approach, webhook_delivery_id="test")
    context = RepositoryAnalysisContext(
        issue_context=IssueContext(repository_owner="atul812", repository_name="demo_repo", issue_number=2,
                                   title="Fix auth and user lookup", body=ISSUE_BODY),
        repository_context=RepositoryContext(owner="atul812", name="demo_repo"),
        code_context=CodeContext(relevant_files=["app.py"], file_contents=[CodeFile(path="app.py", content=APP_SOURCE)]),
    )
    analysis = await DefaultAnalysisService(PassWithoutCitations()).analyze(candidate, context)
    assert analysis.decision is expected
    if expected is AnalysisDecision.REVISION_REQUIRED:
        assert "401" in analysis.revision_feedback
    if expected is AnalysisDecision.PASS:
        assert analysis.evidence


@pytest.mark.asyncio
async def test_complete_grounded_plan_is_not_revised_for_zero_confidence_model_output():
    approach = FIRST_APPROACH
    candidate = CandidateSubmission(repository_owner="atul812", repository_name="demo_repo", issue_number=2,
                                    contributor_username="contributor", comment_id=1, comment_body=approach,
                                    approach=approach, webhook_delivery_id="zero-confidence")
    context = RepositoryAnalysisContext(
        issue_context=IssueContext(repository_owner="atul812", repository_name="demo_repo", issue_number=2,
                                   title="Fix auth and user lookup", body=ISSUE_BODY),
        repository_context=RepositoryContext(owner="atul812", name="demo_repo"),
        code_context=CodeContext(relevant_files=["app.py"], file_contents=[CodeFile(path="app.py", content=APP_SOURCE)]),
    )
    analysis = await DefaultAnalysisService(ZeroConfidenceRevision()).analyze(candidate, context)
    assert analysis.decision is AnalysisDecision.PASS
    assert analysis.evidence
    assert analysis.confidence == 0.5


@pytest.mark.asyncio
async def test_concrete_confident_objection_is_not_overridden_by_checklist():
    approach = FIRST_APPROACH
    candidate = CandidateSubmission(repository_owner="atul812", repository_name="demo_repo", issue_number=2,
                                    contributor_username="contributor", comment_id=2, comment_body=approach,
                                    approach=approach, webhook_delivery_id="concrete-objection")
    context = RepositoryAnalysisContext(
        issue_context=IssueContext(repository_owner="atul812", repository_name="demo_repo", issue_number=2,
                                   title="Fix auth and user lookup", body=ISSUE_BODY),
        repository_context=RepositoryContext(owner="atul812", name="demo_repo"),
        code_context=CodeContext(relevant_files=["app.py"], file_contents=[CodeFile(path="app.py", content=APP_SOURCE)]),
    )
    analysis = await DefaultAnalysisService(ConfidentConcreteRevision()).analyze(candidate, context)
    assert analysis.decision is AnalysisDecision.REVISION_REQUIRED


@pytest.mark.asyncio
async def test_reasonless_rejection_of_complete_grounded_plan_is_not_posted():
    approach = FIRST_APPROACH
    candidate = CandidateSubmission(repository_owner="atul812", repository_name="demo_repo", issue_number=2,
                                    contributor_username="contributor", comment_id=3, comment_body=approach,
                                    approach=approach, webhook_delivery_id="reasonless-reject")
    context = RepositoryAnalysisContext(
        issue_context=IssueContext(repository_owner="atul812", repository_name="demo_repo", issue_number=2,
                                   title="Fix auth and user lookup", body=ISSUE_BODY),
        repository_context=RepositoryContext(owner="atul812", name="demo_repo"),
        code_context=CodeContext(relevant_files=["app.py"], file_contents=[CodeFile(path="app.py", content=APP_SOURCE)]),
    )
    analysis = await DefaultAnalysisService(RejectionWithoutReason()).analyze(candidate, context)
    assert analysis.decision is AnalysisDecision.PASS


@pytest.mark.asyncio
async def test_invented_evidence_path_is_not_accepted_even_for_complete_checklist():
    approach = FIRST_APPROACH
    candidate = CandidateSubmission(repository_owner="atul812", repository_name="demo_repo", issue_number=2,
                                    contributor_username="contributor", comment_id=4, comment_body=approach,
                                    approach=approach, webhook_delivery_id="invented-path")
    context = RepositoryAnalysisContext(
        issue_context=IssueContext(repository_owner="atul812", repository_name="demo_repo", issue_number=2,
                                   title="Fix auth and user lookup", body=ISSUE_BODY),
        repository_context=RepositoryContext(owner="atul812", name="demo_repo"),
        code_context=CodeContext(relevant_files=["app.py"], file_contents=[CodeFile(path="app.py", content=APP_SOURCE)]),
    )
    with pytest.raises(ValueError, match="absent from supplied context"):
        await DefaultAnalysisService(ZeroConfidenceInventedPath()).analyze(candidate, context)


@pytest.mark.asyncio
async def test_bad_excerpt_from_real_file_is_replaced_by_independent_grounding():
    approach = FIRST_APPROACH
    candidate = CandidateSubmission(repository_owner="atul812", repository_name="demo_repo", issue_number=2,
                                    contributor_username="contributor", comment_id=5, comment_body=approach,
                                    approach=approach, webhook_delivery_id="bad-excerpt")
    context = RepositoryAnalysisContext(
        issue_context=IssueContext(repository_owner="atul812", repository_name="demo_repo", issue_number=2,
                                   title="Fix auth and user lookup", body=ISSUE_BODY),
        repository_context=RepositoryContext(owner="atul812", name="demo_repo"),
        code_context=CodeContext(relevant_files=["app.py"], file_contents=[CodeFile(path="app.py", content=APP_SOURCE)]),
    )
    analysis = await DefaultAnalysisService(InvalidExcerptInRealFile()).analyze(candidate, context)
    assert analysis.decision is AnalysisDecision.PASS
    assert analysis.confidence == 0.5
    assert all(item.startswith("app.py:L") for item in analysis.evidence)


@pytest.mark.asyncio
async def test_unseen_extra_citation_is_discarded_without_hiding_grounded_plan():
    approach = FIRST_APPROACH
    candidate = CandidateSubmission(repository_owner="atul812", repository_name="demo_repo", issue_number=2,
                                    contributor_username="contributor", comment_id=6, comment_body=approach,
                                    approach=approach, webhook_delivery_id="mixed-citations")
    context = RepositoryAnalysisContext(
        issue_context=IssueContext(repository_owner="atul812", repository_name="demo_repo", issue_number=2,
                                   title="Fix auth and user lookup", body=ISSUE_BODY),
        repository_context=RepositoryContext(owner="atul812", name="demo_repo"),
        code_context=CodeContext(relevant_files=["app.py"], file_contents=[CodeFile(path="app.py", content=APP_SOURCE)]),
    )
    analysis = await DefaultAnalysisService(MixedRealAndUnseenCitations()).analyze(candidate, context)
    assert analysis.decision is AnalysisDecision.PASS
    assert all(item.startswith("app.py:L") for item in analysis.evidence)
