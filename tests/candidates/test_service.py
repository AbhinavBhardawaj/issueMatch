import unittest
from app.candidates.repository import InMemoryCandidateRepository
from app.candidates.service import CandidateService, ProcessingOutcome
from app.models.candidate import CandidateStatus
from tests.fakes import FakeAnalysisService, FakeGitHubClient, event
class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_candidate_pipeline_and_duplicate(self):
        analyzer=FakeAnalysisService(); service=CandidateService(InMemoryCandidateRepository(),lambda _: FakeGitHubClient(),analyzer)
        result=await service.process_issue_comment(event()); self.assertEqual(result.outcome,ProcessingOutcome.ANALYZED); self.assertEqual(result.candidate.status,CandidateStatus.ANALYZED); self.assertEqual(len(analyzer.calls),1)
        self.assertEqual((await service.process_issue_comment(event())).outcome,ProcessingOutcome.DUPLICATE)
    async def test_ordinary_and_failures_safe(self):
        repo=InMemoryCandidateRepository(); service=CandidateService(repo,lambda _: FakeGitHubClient(),FakeAnalysisService())
        self.assertEqual((await service.process_issue_comment(event("ordinary","Good catch."))).outcome,ProcessingOutcome.IGNORED)
        failing=CandidateService(InMemoryCandidateRepository(),lambda _: FakeGitHubClient(fail=True),FakeAnalysisService())
        self.assertEqual((await failing.process_issue_comment(event("fail"))).candidate.status,CandidateStatus.FAILED)
        analysis_failing=CandidateService(InMemoryCandidateRepository(),lambda _: FakeGitHubClient(),FakeAnalysisService(fail=True))
        self.assertEqual((await analysis_failing.process_issue_comment(event("analysis"))).candidate.status,CandidateStatus.FAILED)
