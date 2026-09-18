"""Candidate pipeline orchestration; intentionally independent of AI implementation details."""
from dataclasses import dataclass
from enum import Enum
import logging
from datetime import datetime, timezone
from inspect import isawaitable

from app.candidates.extractor import extract_candidate
from app.candidates.repository import CandidateRepository
from app.context.code_context import collect_code_context
from app.context.issue_context import collect_issue_context
from app.context.repository_context import collect_repository_context
from app.github.client import GitHubClient
from app.github.events import GitHubIssueCommentEvent
from app.models.analysis import AnalysisDecision, AnalysisService, ApproachAnalysis
from app.models.candidate import CandidateStatus, CandidateSubmission
from app.models.context import RepositoryAnalysisContext

logger = logging.getLogger(__name__)


class ProcessingOutcome(str, Enum):
    IGNORED = "IGNORED"
    DUPLICATE = "DUPLICATE"
    ANALYSIS_PENDING = "ANALYSIS_PENDING"
    ANALYZED = "ANALYZED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class CandidateProcessingResult:
    outcome: ProcessingOutcome
    candidate: CandidateSubmission | None = None
    analysis: ApproachAnalysis | None = None
    detail: str = ""


class CandidateService:
    """Owns lifecycle state from normalized webhook event through analysis invocation."""
    def __init__(self, repository: CandidateRepository, github_client_factory, analysis_service: AnalysisService | None = None) -> None:
        self._repository, self._client_factory, self._analysis = repository, github_client_factory, analysis_service

    async def process_issue_comment(self, event: GitHubIssueCommentEvent) -> CandidateProcessingResult:
        if await self._repository.has_delivery(event.delivery_id):
            return CandidateProcessingResult(ProcessingOutcome.DUPLICATE, detail="delivery already processed")
        extracted = extract_candidate(event.comment_body)
        if not extracted.is_candidate:
            await self._repository.mark_delivery(event.delivery_id)
            return CandidateProcessingResult(ProcessingOutcome.IGNORED, detail=extracted.reason)
        candidate = CandidateSubmission(repository_owner=event.repository_owner, repository_name=event.repository_name,
            issue_number=event.issue_number, issue_title=event.issue_title, issue_body=event.issue_body,
            contributor_username=event.comment_author, contributor_github_id=event.comment_author_id,
            comment_id=event.comment_id, comment_body=event.comment_body, approach=extracted.approach_text,
            submitted_at=event.comment_created_at or datetime.now(timezone.utc),
            status=CandidateStatus.CONTEXT_PENDING, webhook_delivery_id=event.delivery_id)
        try:
            await self._repository.create(candidate)
        except ValueError:
            return CandidateProcessingResult(ProcessingOutcome.DUPLICATE, detail="delivery already processed")
        try:
            created_client = self._client_factory(event.installation_id)
            client: GitHubClient = await created_client if isawaitable(created_client) else created_client
            issue = await collect_issue_context(client, event.repository_owner, event.repository_name, event.issue_number)
            repo = await collect_repository_context(client, event.repository_owner, event.repository_name, event.repository_default_branch)
            code = await collect_code_context(client, event.repository_owner, event.repository_name, candidate.approach, repo)
            context = RepositoryAnalysisContext(issue_context=issue, repository_context=repo, code_context=code)
        except Exception:
            logger.exception("Candidate context collection failed for candidate %s", candidate.candidate_id)
            failed = await self._repository.update_status(candidate.candidate_id, CandidateStatus.FAILED)
            return CandidateProcessingResult(ProcessingOutcome.FAILED, candidate=failed, detail="context collection failed")
        pending = await self._repository.update_status(candidate.candidate_id, CandidateStatus.ANALYSIS_PENDING)
        if self._analysis is None:
            return CandidateProcessingResult(ProcessingOutcome.ANALYSIS_PENDING, candidate=pending, detail="analysis service not configured")
        try:
            analysis = await self._analysis.analyze(pending, context)
            # Defend against implementations returning an unvalidated look-alike.
            analysis = ApproachAnalysis.model_validate(analysis)
        except Exception:
            logger.exception("Analysis service failed for candidate %s", candidate.candidate_id)
            failed = await self._repository.update_status(candidate.candidate_id, CandidateStatus.FAILED)
            return CandidateProcessingResult(ProcessingOutcome.FAILED, candidate=failed, detail="analysis service failed")
        status = {AnalysisDecision.PASS: CandidateStatus.ANALYZED, AnalysisDecision.REVISION_REQUIRED: CandidateStatus.REVISION_REQUESTED, AnalysisDecision.REJECT: CandidateStatus.REJECTED}[analysis.decision]
        completed = await self._repository.update_status(candidate.candidate_id, status)
        return CandidateProcessingResult(ProcessingOutcome.ANALYZED, candidate=completed, analysis=analysis)
