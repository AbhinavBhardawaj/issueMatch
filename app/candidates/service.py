"""Ordered candidate evaluation and issue-level recommendation orchestration."""
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from inspect import isawaitable
import logging

from app.candidates.extractor import CandidateKind, extract_candidate
from app.candidates.repository import CandidateRepository
from app.context.code_context import collect_code_context
from app.context.issue_context import collect_issue_context
from app.context.repository_context import collect_repository_context
from app.github.client import GitHubClient
from app.github.events import GitHubIssueAssignmentEvent, GitHubIssueCommentEvent
from app.github.response import post_analysis_unavailable, post_candidate_analysis
from app.models.analysis import AnalysisDecision, AnalysisService, ApproachAnalysis
from app.models.candidate import CandidateStatus, CandidateSubmission, IssueCandidateState, IssueEvaluation
from app.models.context import RepositoryAnalysisContext

logger = logging.getLogger(__name__)

_SAFE_ANALYSIS_FAILURES = {
    "Analysis provider returned invalid structured output",
    "Provider cited a repository file absent from supplied context",
    "Provider PASS cited a repository file absent from supplied context",
    "Provider PASS cited no verifiable repository source",
    "Provider PASS lacked relevant repository grounding",
    "Provider PASS had zero confidence",
    "Complete checklist lacked relevant repository grounding",
    "Provider revision lacked actionable feedback",
    "Provider rejection lacked concrete reasons",
}


class ProcessingOutcome(str, Enum):
    IGNORED = "IGNORED"
    DUPLICATE = "DUPLICATE"
    WAITING = "WAITING"
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
    """Coordinates claims, ordered approaches, analysis, and assignment changes."""

    def __init__(self, repository: CandidateRepository, github_client_factory,
                 analysis_service: AnalysisService | None = None,
                 analysis_response_poster=post_candidate_analysis) -> None:
        self._repository = repository
        self._client_factory = github_client_factory
        self._analysis = analysis_service
        self._analysis_response_poster = analysis_response_poster

    async def process_issue_comment(self, event: GitHubIssueCommentEvent) -> CandidateProcessingResult:
        """Process one comment; claim-only comments never invoke analysis."""
        if await self._repository.has_delivery(event.delivery_id):
            logger.info("Duplicate webhook ignored: delivery=%s", event.delivery_id)
            return CandidateProcessingResult(ProcessingOutcome.DUPLICATE, detail="delivery already processed")
        if await self._find_by_comment(event.repository_owner, event.repository_name, event.issue_number, event.comment_id):
            await self._repository.mark_delivery(event.delivery_id)
            logger.info("Duplicate comment ignored: comment=%s", event.comment_id)
            return CandidateProcessingResult(ProcessingOutcome.DUPLICATE, detail="comment already processed")

        extraction = extract_candidate(event.comment_body)
        if extraction.kind is not CandidateKind.APPROACH_SUBMITTED:
            await self._repository.mark_delivery(event.delivery_id)
            if extraction.kind is CandidateKind.CLAIM_ONLY:
                state = await self._get_issue_state(event.repository_owner, event.repository_name, event.issue_number)
                if state.status is IssueCandidateState.WAITING_FOR_CANDIDATES:
                    await self._save_issue_state(state)
            if extraction.kind is CandidateKind.CLAIM_ONLY:
                logger.info("Claim-only comment ignored: comment=%s", event.comment_id)
            return CandidateProcessingResult(ProcessingOutcome.IGNORED, detail=extraction.reason)

        issue_state = await self._get_issue_state(event.repository_owner, event.repository_name, event.issue_number)
        parent = await self._find_revision_parent(event)
        selected = issue_state.status in {IssueCandidateState.CANDIDATE_RECOMMENDED, IssueCandidateState.ASSIGNED}
        status = CandidateStatus.WAITING_FOR_EVALUATION if selected else CandidateStatus.APPROACH_SUBMITTED
        candidate = CandidateSubmission(
            repository_owner=event.repository_owner, repository_name=event.repository_name,
            issue_number=event.issue_number, issue_title=event.issue_title, issue_body=event.issue_body,
            contributor_username=event.comment_author, contributor_github_id=event.comment_author_id,
            comment_id=event.comment_id, comment_body=event.comment_body,
            approach=extraction.approach_text,
            submitted_at=event.comment_created_at or datetime.now(timezone.utc),
            status=status, webhook_delivery_id=event.delivery_id,
            parent_candidate_id=parent.candidate_id if parent else None,
            priority=issue_state.next_priority,
        )
        try:
            await self._repository.create(candidate)
        except ValueError:
            return CandidateProcessingResult(ProcessingOutcome.DUPLICATE, detail="delivery already processed")

        await self._save_issue_state(issue_state.model_copy(update={
            "next_priority": issue_state.next_priority + 1,
            "status": issue_state.status if selected else IssueCandidateState.EVALUATING_CANDIDATES,
            "updated_at": datetime.now(timezone.utc),
        }))
        logger.info("Approach candidate created: candidate=%s priority=%s", candidate.candidate_id, candidate.priority)
        if selected:
            logger.info("Candidate skipped while another candidate is selected: candidate=%s", candidate.candidate_id)
            return CandidateProcessingResult(ProcessingOutcome.WAITING, candidate=candidate, detail="another candidate is recommended or assigned")
        return await self._evaluate_candidate(candidate, event.installation_id, event.repository_default_branch)

    async def resume_incomplete_issue_comment(self, event: GitHubIssueCommentEvent) -> CandidateProcessingResult:
        """Resume only a queued retry of the *same* partially saved delivery.

        Ordinary GitHub redeliveries still use process_issue_comment and are
        ignored. The durable worker calls this only after an earlier attempt
        failed, so a storage error after candidate creation does not turn its
        next attempt into a no-op duplicate.
        """
        candidate = await self._repository.get_for_delivery(event.delivery_id)
        if candidate is None:
            return await self.process_issue_comment(event)
        if (candidate.comment_id != event.comment_id
                or candidate.repository_owner != event.repository_owner
                or candidate.repository_name != event.repository_name
                or candidate.issue_number != event.issue_number):
            raise ValueError("Delivery candidate does not match webhook event")
        if candidate.status not in {
            CandidateStatus.APPROACH_SUBMITTED, CandidateStatus.ANALYZING,
            CandidateStatus.CONTEXT_PENDING, CandidateStatus.ANALYSIS_PENDING,
        }:
            logger.info("Completed or ineligible candidate retry ignored: candidate=%s status=%s",
                        candidate.candidate_id, candidate.status.value)
            return CandidateProcessingResult(ProcessingOutcome.DUPLICATE, candidate=candidate,
                                             detail="candidate already processed")
        if candidate.status is CandidateStatus.APPROACH_SUBMITTED:
            state = await self._get_issue_state(candidate.repository_owner, candidate.repository_name,
                                                candidate.issue_number)
            if candidate.priority is not None and state.next_priority <= candidate.priority:
                await self._save_issue_state(state.model_copy(update={
                    "next_priority": candidate.priority + 1,
                    "status": IssueCandidateState.EVALUATING_CANDIDATES,
                    "updated_at": datetime.now(timezone.utc),
                }))
        logger.info("Resuming incomplete candidate delivery: candidate=%s", candidate.candidate_id)
        return await self._evaluate_candidate(candidate, event.installation_id,
                                              event.repository_default_branch)

    async def process_issue_assignment(self, event: GitHubIssueAssignmentEvent) -> CandidateProcessingResult:
        """Track assignment changes and resume ordered evaluation after unassignment."""
        if await self._repository.has_delivery(event.delivery_id):
            logger.info("Duplicate assignment webhook ignored: delivery=%s", event.delivery_id)
            return CandidateProcessingResult(ProcessingOutcome.DUPLICATE, detail="delivery already processed")
        state = await self._get_issue_state(event.repository_owner, event.repository_name, event.issue_number)
        candidates = await self._repository.list_for_issue(event.repository_owner, event.repository_name, event.issue_number)
        target = next((candidate for candidate in candidates if (
            candidate.contributor_github_id == event.assignee_github_id
            or candidate.contributor_username.lower() == event.assignee_username.lower()
        ) and (candidate.is_recommended or candidate.status in {CandidateStatus.ACCEPTED, CandidateStatus.RECOMMENDED, CandidateStatus.ASSIGNED})), None)

        if event.action == "assigned":
            updated_candidate = None
            if target:
                updated_candidate = await self._repository.update(target.model_copy(update={
                    "status": CandidateStatus.ASSIGNED, "is_assigned": True,
                    "assigned_at": datetime.now(timezone.utc),
                }))
            await self._save_issue_state(state.model_copy(update={
                "status": IssueCandidateState.ASSIGNED,
                "assigned_candidate_id": updated_candidate.candidate_id if updated_candidate else None,
                "assigned_username": event.assignee_username,
                "updated_at": datetime.now(timezone.utc),
            }))
            await self._repository.mark_delivery(event.delivery_id)
            logger.info("Assignment detected: issue=%s/%s#%s assignee=%s", event.repository_owner, event.repository_name, event.issue_number, event.assignee_username)
            return CandidateProcessingResult(ProcessingOutcome.WAITING, candidate=updated_candidate, detail="assignment recorded")

        if target:
            target = await self._repository.update(target.model_copy(update={
                "status": CandidateStatus.UNASSIGNED, "is_assigned": False,
                "is_recommended": False, "unassigned_at": datetime.now(timezone.utc),
            }))
        await self._save_issue_state(state.model_copy(update={
            "status": IssueCandidateState.RESUME_EVALUATION,
            "recommended_candidate_id": None, "assigned_candidate_id": None,
            "assigned_username": None, "updated_at": datetime.now(timezone.utc),
        }))
        await self._repository.mark_delivery(event.delivery_id)
        logger.info("Unassignment detected; resuming candidate evaluation: issue=%s/%s#%s", event.repository_owner, event.repository_name, event.issue_number)
        return await self._evaluate_next_pending(event)

    async def _evaluate_candidate(self, candidate: CandidateSubmission, installation_id: int | None, default_branch: str) -> CandidateProcessingResult:
        candidate = await self._repository.update(candidate.model_copy(update={"status": CandidateStatus.ANALYZING}))
        try:
            client = await self._make_client(installation_id)
            context = await self._collect_context(client, candidate, default_branch)
        except Exception as exc:
            logger.error("Candidate context collection failed: candidate=%s failure_type=%s", candidate.candidate_id, type(exc).__name__)
            failed = await self._repository.update_status(candidate.candidate_id, CandidateStatus.FAILED)
            return CandidateProcessingResult(ProcessingOutcome.FAILED, candidate=failed, detail=f"context collection failed ({type(exc).__name__})")
        if self._analysis is None:
            pending = await self._repository.update_status(candidate.candidate_id, CandidateStatus.ANALYSIS_PENDING)
            return CandidateProcessingResult(ProcessingOutcome.ANALYSIS_PENDING, candidate=pending, detail="analysis service not configured")
        try:
            analysis = ApproachAnalysis.model_validate(await self._analysis.analyze(candidate, context))
        except Exception as exc:
            failure_reason = str(exc) if type(exc) is ValueError and str(exc) in _SAFE_ANALYSIS_FAILURES else type(exc).__name__
            if hasattr(exc, "errors") and type(exc).__name__ == "ValidationError":
                # Log schema locations and rule types only. Pydantic's full
                # messages/input can contain untrusted source or credentials.
                entries = exc.errors(include_input=False, include_context=False)
                schema = getattr(exc, "title", "")
                schema = schema if schema in {"ApproachAnalysis", "AnalysisProviderResult", "EvidenceCitation"} else "Schema"
                fields = ";".join(
                    f"{'.'.join(str(part) for part in entry.get('loc', ())[:3]) or 'model'}:{entry.get('type', 'invalid')}"
                    for entry in entries[:3]
                ) or "model:invalid"
                failure_reason = f"{schema}/{fields}"
            logger.error("Analysis service failed: candidate=%s failure_type=%s reason=%s",
                         candidate.candidate_id, type(exc).__name__, failure_reason)
            failed = await self._repository.update_status(candidate.candidate_id, CandidateStatus.FAILED)
            try:
                await post_analysis_unavailable(client, failed)
            except Exception as posting_exc:
                logger.error("Analysis-unavailable notice failed: candidate=%s failure_type=%s",
                             candidate.candidate_id, type(posting_exc).__name__)
            return CandidateProcessingResult(ProcessingOutcome.FAILED, candidate=failed, detail=f"analysis service failed ({type(exc).__name__})")

        status = {
            AnalysisDecision.PASS: CandidateStatus.ACCEPTED,
            AnalysisDecision.REVISION_REQUIRED: CandidateStatus.REVISION_REQUIRED,
            AnalysisDecision.REJECT: CandidateStatus.DECLINED,
        }[analysis.decision]
        completed = await self._repository.update(candidate.model_copy(update={
            "status": status, "analysis_decision": analysis.decision.value,
            "analysis_result": analysis.model_dump(mode="json"),
            "is_recommended": analysis.decision is AnalysisDecision.PASS,
        }))
        if analysis.decision is AnalysisDecision.PASS:
            state = await self._get_issue_state(candidate.repository_owner, candidate.repository_name, candidate.issue_number)
            await self._save_issue_state(state.model_copy(update={
                "status": IssueCandidateState.CANDIDATE_RECOMMENDED,
                "recommended_candidate_id": completed.candidate_id,
                "updated_at": datetime.now(timezone.utc),
            }))
            logger.info("Candidate accepted and recommended: candidate=%s", completed.candidate_id)
        else:
            logger.info("Candidate analyzed: candidate=%s decision=%s", completed.candidate_id, analysis.decision.value)
            remaining = await self._repository.list_for_issue(candidate.repository_owner, candidate.repository_name, candidate.issue_number)
            if not any(item.status in {CandidateStatus.WAITING_FOR_EVALUATION, CandidateStatus.APPROACH_SUBMITTED} for item in remaining):
                state = await self._get_issue_state(candidate.repository_owner, candidate.repository_name, candidate.issue_number)
                await self._save_issue_state(state.model_copy(update={
                    "status": IssueCandidateState.WAITING_FOR_CANDIDATES,
                    "updated_at": datetime.now(timezone.utc),
                }))
        try:
            await self._analysis_response_poster(client, completed, analysis)
            logger.info("Maintainer notification posted: candidate=%s", completed.candidate_id)
        except Exception:
            logger.exception("Posting analysis response failed for candidate %s", completed.candidate_id)
        return CandidateProcessingResult(ProcessingOutcome.ANALYZED, candidate=completed, analysis=analysis)

    async def _evaluate_next_pending(self, event: GitHubIssueAssignmentEvent) -> CandidateProcessingResult:
        candidates = sorted(
            await self._repository.list_for_issue(event.repository_owner, event.repository_name, event.issue_number),
            key=lambda candidate: (candidate.priority or 10**9, candidate.submitted_at),
        )
        for candidate in candidates:
            if candidate.status not in {CandidateStatus.WAITING_FOR_EVALUATION, CandidateStatus.APPROACH_SUBMITTED}:
                continue
            result = await self._evaluate_candidate(candidate, event.installation_id, event.repository_default_branch)
            if result.outcome is not ProcessingOutcome.ANALYZED or result.candidate is None:
                return result
            if result.candidate.status is CandidateStatus.ACCEPTED:
                return result
        state = await self._get_issue_state(event.repository_owner, event.repository_name, event.issue_number)
        await self._save_issue_state(state.model_copy(update={
            "status": IssueCandidateState.WAITING_FOR_CANDIDATES,
            "recommended_candidate_id": None, "updated_at": datetime.now(timezone.utc),
        }))
        return CandidateProcessingResult(ProcessingOutcome.WAITING, detail="no eligible candidates remain")

    async def _collect_context(self, client: GitHubClient, candidate: CandidateSubmission, default_branch: str) -> RepositoryAnalysisContext:
        issue = await collect_issue_context(client, candidate.repository_owner, candidate.repository_name, candidate.issue_number)
        repository = await collect_repository_context(client, candidate.repository_owner, candidate.repository_name, default_branch)
        code = await collect_code_context(client, candidate.repository_owner, candidate.repository_name, candidate.approach, repository, issue_context=issue)
        return RepositoryAnalysisContext(issue_context=issue, repository_context=repository, code_context=code)

    async def _make_client(self, installation_id: int | None) -> GitHubClient:
        created = self._client_factory(installation_id)
        return await created if isawaitable(created) else created

    async def _get_issue_state(self, owner: str, repository: str, issue_number: int) -> IssueEvaluation:
        getter = getattr(self._repository, "get_issue_state", None)
        state = await getter(owner, repository, issue_number) if getter else None
        return state or IssueEvaluation(repository_owner=owner, repository_name=repository, issue_number=issue_number)

    async def _save_issue_state(self, state: IssueEvaluation) -> IssueEvaluation:
        updater = getattr(self._repository, "update_issue_state", None)
        if updater:
            return await updater(state)
        return state

    async def _find_by_comment(self, owner: str, repository: str, issue_number: int, comment_id: int) -> CandidateSubmission | None:
        finder = getattr(self._repository, "find_by_comment", None)
        if finder:
            return await finder(owner, repository, issue_number, comment_id)
        return next((candidate for candidate in await self._repository.list_for_issue(owner, repository, issue_number) if candidate.comment_id == comment_id), None)

    async def _find_revision_parent(self, event: GitHubIssueCommentEvent) -> CandidateSubmission | None:
        candidates = await self._repository.list_for_issue(event.repository_owner, event.repository_name, event.issue_number)
        previous = [candidate for candidate in candidates if candidate.contributor_username.lower() == event.comment_author.lower() and candidate.status is CandidateStatus.REVISION_REQUIRED]
        return max(previous, key=lambda candidate: candidate.submitted_at, default=None)
