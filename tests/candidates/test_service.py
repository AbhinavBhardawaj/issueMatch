import unittest
from app.candidates.repository import InMemoryCandidateRepository
from app.candidates.service import CandidateService, ProcessingOutcome
from app.github.events import GitHubIssueAssignmentEvent
from app.models.analysis import AnalysisDecision, ApproachAnalysis
from app.models.candidate import CandidateStatus, IssueCandidateState
from tests.fakes import FakeAnalysisService, FakeGitHubClient, event


class DecisionAnalysis:
    def __init__(self, decisions): self.decisions, self.calls = decisions, []
    async def analyze(self, candidate, context):
        self.calls.append(candidate)
        decision = self.decisions.get(candidate.contributor_username, AnalysisDecision.PASS)
        if decision is AnalysisDecision.PASS:
            return ApproachAnalysis(decision=decision, confidence=.9, evidence=["src/auth/session.py"], recommendation="Maintainer may consider assignment.")
        if decision is AnalysisDecision.REVISION_REQUIRED:
            return ApproachAnalysis(decision=decision, confidence=.5, revision_feedback="Please explain the validation path.")
        return ApproachAnalysis(decision=decision, confidence=.1, issues=["The proposed component is unrelated."], recommendation="Do not assign based on this approach.")


def assignment(delivery, action, username="alice"):
    return GitHubIssueAssignmentEvent(delivery_id=delivery, action=action, repository_owner="acme", repository_name="demo", installation_id=1, issue_number=7, issue_title="Session timeout", assignee_username=username, assignee_github_id=9, repository_default_branch="main")


def approach_event(delivery, username, comment="I'd like to work on this. I'll modify src/auth/session.py and add a regression test.", comment_id=None):
    current = event(delivery, comment)
    return current.model_copy(update={"comment_author": username, "comment_id": comment_id or abs(hash(delivery))})
class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_candidate_pipeline_and_duplicate(self):
        analyzer=FakeAnalysisService(); github=FakeGitHubClient()
        service=CandidateService(InMemoryCandidateRepository(),lambda _: github,analyzer)
        result=await service.process_issue_comment(event()); self.assertEqual(result.outcome,ProcessingOutcome.ANALYZED); self.assertEqual(result.candidate.status,CandidateStatus.ACCEPTED); self.assertEqual(len(analyzer.calls),1)
        self.assertEqual(len(github.comments), 1); self.assertIn("Status - ACCEPTED", github.comments[0])
        self.assertEqual((await service.process_issue_comment(event())).outcome,ProcessingOutcome.DUPLICATE)
    async def test_ordinary_and_failures_safe(self):
        repo=InMemoryCandidateRepository(); service=CandidateService(repo,lambda _: FakeGitHubClient(),FakeAnalysisService())
        self.assertEqual((await service.process_issue_comment(event("ordinary","Good catch."))).outcome,ProcessingOutcome.IGNORED)
        failing=CandidateService(InMemoryCandidateRepository(),lambda _: FakeGitHubClient(fail=True),FakeAnalysisService())
        self.assertEqual((await failing.process_issue_comment(event("fail"))).candidate.status,CandidateStatus.FAILED)
        analysis_failing=CandidateService(InMemoryCandidateRepository(),lambda _: FakeGitHubClient(),FakeAnalysisService(fail=True))
        self.assertEqual((await analysis_failing.process_issue_comment(event("analysis"))).candidate.status,CandidateStatus.FAILED)

    async def test_claim_only_and_ordered_candidates(self):
        repository = InMemoryCandidateRepository(); analyzer = DecisionAnalysis({"bob": AnalysisDecision.REJECT, "carol": AnalysisDecision.PASS, "dana": AnalysisDecision.PASS})
        github = FakeGitHubClient(); service = CandidateService(repository, lambda _: github, analyzer)
        self.assertEqual((await service.process_issue_comment(event("claim", "I want to work on this issue."))).outcome, ProcessingOutcome.IGNORED)
        self.assertEqual(analyzer.calls, [])
        self.assertEqual(github.comments, [])
        bob = await service.process_issue_comment(approach_event("b", "bob")); carol = await service.process_issue_comment(approach_event("c", "carol")); dana = await service.process_issue_comment(approach_event("d", "dana"))
        self.assertEqual(bob.candidate.status, CandidateStatus.DECLINED)
        self.assertEqual(carol.candidate.status, CandidateStatus.ACCEPTED)
        self.assertEqual(dana.outcome, ProcessingOutcome.WAITING)
        self.assertEqual([candidate.contributor_username for candidate in analyzer.calls], ["bob", "carol"])
        self.assertEqual((await repository.get_issue_state("acme", "demo", 7)).status, IssueCandidateState.CANDIDATE_RECOMMENDED)
        self.assertEqual(len(github.comments), 2)

    async def test_revision_is_analyzed_again_and_unassignment_resumes(self):
        repository = InMemoryCandidateRepository(); analyzer = DecisionAnalysis({"alice": AnalysisDecision.PASS, "bob": AnalysisDecision.PASS})
        service = CandidateService(repository, lambda _: FakeGitHubClient(), analyzer)
        accepted = await service.process_issue_comment(approach_event("a", "alice"))
        waiting = await service.process_issue_comment(approach_event("b", "bob"))
        self.assertEqual(accepted.candidate.status, CandidateStatus.ACCEPTED); self.assertEqual(waiting.outcome, ProcessingOutcome.WAITING)
        await service.process_issue_assignment(assignment("assigned", "assigned"))
        resumed = await service.process_issue_assignment(assignment("unassigned", "unassigned"))
        self.assertEqual(resumed.candidate.contributor_username, "bob"); self.assertEqual(resumed.candidate.status, CandidateStatus.ACCEPTED)
        candidates = await repository.list_for_issue("acme", "demo", 7)
        self.assertEqual(next(c.status for c in candidates if c.contributor_username == "alice"), CandidateStatus.UNASSIGNED)

    async def test_revision_comment_creates_a_new_revision_candidate(self):
        repository = InMemoryCandidateRepository(); analyzer = DecisionAnalysis({"alice": AnalysisDecision.REVISION_REQUIRED})
        service = CandidateService(repository, lambda _: FakeGitHubClient(), analyzer)
        first = await service.process_issue_comment(approach_event("r1", "alice"))
        second = await service.process_issue_comment(approach_event("r2", "alice", "I will modify src/auth/session.py and add a regression test."))
        self.assertEqual(first.candidate.status, CandidateStatus.REVISION_REQUIRED)
        self.assertEqual(second.candidate.parent_candidate_id, first.candidate.candidate_id)
        self.assertEqual(len(analyzer.calls), 2)
