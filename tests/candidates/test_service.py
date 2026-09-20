import unittest
from app.analysis.provider import AnalysisProviderResult, EvidenceCitation
from app.analysis.service import DefaultAnalysisService
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
    async def test_retry_repairs_issue_priority_after_state_write_failure(self):
        class FailingOnceRepository(InMemoryCandidateRepository):
            def __init__(self):
                super().__init__()
                self.failed_once = False

            async def update_issue_state(self, state):
                if not self.failed_once:
                    self.failed_once = True
                    raise RuntimeError("transient issue-state write failure")
                return await super().update_issue_state(state)

        repository = FailingOnceRepository()
        github = FakeGitHubClient()
        service = CandidateService(repository, lambda _: github, FakeAnalysisService())
        comment = event("retry-after-state-write")

        with self.assertRaisesRegex(RuntimeError, "transient issue-state write failure"):
            await service.process_issue_comment(comment)
        stored = await repository.get_for_delivery(comment.delivery_id)
        self.assertEqual(stored.status, CandidateStatus.APPROACH_SUBMITTED)

        result = await service.resume_incomplete_issue_comment(comment)
        self.assertEqual(result.candidate.status, CandidateStatus.ACCEPTED)
        state = await repository.get_issue_state("acme", "demo", 7)
        self.assertEqual(state.next_priority, 2)
        self.assertEqual(len(github.comments), 1)

    async def test_retry_recovers_candidate_after_analysis_write_failure(self):
        class FailingOnceRepository(InMemoryCandidateRepository):
            def __init__(self):
                super().__init__()
                self.failed_once = False

            async def update(self, candidate):
                if candidate.status is CandidateStatus.ACCEPTED and not self.failed_once:
                    self.failed_once = True
                    raise RuntimeError("transient candidate write failure")
                return await super().update(candidate)

        repository = FailingOnceRepository()
        github = FakeGitHubClient()
        analyzer = FakeAnalysisService()
        service = CandidateService(repository, lambda _: github, analyzer)
        comment = event("retry-after-write")

        with self.assertRaisesRegex(RuntimeError, "transient candidate write failure"):
            await service.process_issue_comment(comment)
        stored = await repository.get_for_delivery(comment.delivery_id)
        self.assertEqual(stored.status, CandidateStatus.ANALYZING)
        self.assertEqual((await service.process_issue_comment(comment)).outcome,
                         ProcessingOutcome.DUPLICATE)

        recovered = await service.resume_incomplete_issue_comment(comment)
        self.assertEqual(recovered.outcome, ProcessingOutcome.ANALYZED)
        self.assertEqual(recovered.candidate.status, CandidateStatus.ACCEPTED)
        self.assertEqual(len(github.comments), 1)
        self.assertIn("Status - ACCEPTED", github.comments[0])
        self.assertEqual((await service.resume_incomplete_issue_comment(comment)).outcome,
                         ProcessingOutcome.DUPLICATE)
        self.assertEqual(len(github.comments), 1)

    async def test_citation_repair_that_changes_decision_still_posts_valid_result(self):
        class SequencedProvider:
            def __init__(self):
                self.calls = 0

            async def generate(self, prompt):
                self.calls += 1
                if self.calls == 2:
                    return AnalysisProviderResult(decision=AnalysisDecision.REVISION_REQUIRED,
                                                  confidence=0.5)
                return AnalysisProviderResult(
                    decision=AnalysisDecision.PASS, confidence=0.8,
                    evidence=[EvidenceCitation(
                        path="src/auth/session.py",
                        excerpt="missing source" if self.calls == 1 else "def timeout(): pass",
                        claim="The timeout implementation is in this file.",
                    )],
                )

        github = FakeGitHubClient()
        provider = SequencedProvider()
        service = CandidateService(InMemoryCandidateRepository(), lambda _: github,
                                   DefaultAnalysisService(provider))
        result = await service.process_issue_comment(event("citation-repair-flow"))

        self.assertEqual(result.outcome, ProcessingOutcome.ANALYZED)
        self.assertEqual(result.candidate.status, CandidateStatus.ACCEPTED)
        self.assertEqual(provider.calls, 3)
        self.assertEqual(len(github.comments), 1)
        self.assertIn("Status - ACCEPTED", github.comments[0])

    async def test_issue_only_root_file_reaches_analysis_service(self):
        class DemoClient(FakeGitHubClient):
            async def get_issue(self, *args):
                return {"title": "Fix Flask backend", "body": "The login error in app.py returns 200 instead of 401.", "state": "open"}

        github = DemoClient(files={"README.md": "Demo", "app.py": "def login():\n    return 'error', 200"})
        analyzer = FakeAnalysisService()
        service = CandidateService(InMemoryCandidateRepository(), lambda _: github, analyzer)
        comment = "I want to work on this. I'll change login failures to return 401 and add a regression test."
        result = await service.process_issue_comment(event("issue-root", comment))
        self.assertEqual(result.outcome, ProcessingOutcome.ANALYZED)
        self.assertEqual([file.path for file in analyzer.calls[0][1].code_context.file_contents], ["app.py"])

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

    async def test_provider_validation_failure_does_not_blame_contributor(self):
        class InvalidProvider:
            async def analyze(self, candidate, context):
                raise ValueError("Model citation could not be verified")

        github = FakeGitHubClient()
        service = CandidateService(InMemoryCandidateRepository(), lambda _: github, InvalidProvider())
        result = await service.process_issue_comment(event("invalid-provider"))
        self.assertEqual(result.outcome, ProcessingOutcome.FAILED)
        self.assertEqual(result.candidate.status, CandidateStatus.FAILED)
        self.assertEqual(len(github.comments), 1)
        self.assertIn("Analysis temporarily unavailable", github.comments[0])
        self.assertNotIn("Status - REVISION_REQUIRED", github.comments[0])
        self.assertEqual((await service.process_issue_comment(event("invalid-provider"))).outcome,
                         ProcessingOutcome.DUPLICATE)
        self.assertEqual(len(github.comments), 1)

    async def test_validation_log_reports_rule_without_echoing_input(self):
        import logging
        from pydantic import ValidationError

        class InvalidProvider:
            async def analyze(self, candidate, context):
                raise ValidationError.from_exception_data(
                    "AnalysisProviderResult",
                    [{"type": "string_type", "loc": ("evidence", 0, "path"),
                      "input": "private-source-content"}],
                )

        service = CandidateService(InMemoryCandidateRepository(),
                                   lambda _: FakeGitHubClient(), InvalidProvider())
        with self.assertLogs("app.candidates.service", level=logging.ERROR) as captured:
            result = await service.process_issue_comment(event("validation-log"))
        self.assertEqual(result.outcome, ProcessingOutcome.FAILED)
        self.assertIn("evidence.0.path:string_type", captured.output[0])
        self.assertNotIn("private-source-content", captured.output[0])

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
