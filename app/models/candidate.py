"""Candidate-submission domain types."""
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class CandidateStatus(str, Enum):
    CLAIM_ONLY = "CLAIM_ONLY"
    APPROACH_SUBMITTED = "APPROACH_SUBMITTED"
    ANALYZING = "ANALYZING"
    ACCEPTED = "ACCEPTED"
    ANALYZED = "ACCEPTED"  # backwards-compatible name from the original pipeline
    REVISION_REQUIRED = "REVISION_REQUIRED"
    REVISION_REQUESTED = "REVISION_REQUIRED"  # backwards-compatible name
    DECLINED = "DECLINED"
    REJECTED = "DECLINED"  # backwards-compatible name
    RECOMMENDED = "RECOMMENDED"
    ASSIGNED = "ASSIGNED"
    UNASSIGNED = "UNASSIGNED"
    SUPERSEDED = "SUPERSEDED"
    WAITING_FOR_EVALUATION = "WAITING_FOR_EVALUATION"
    RECEIVED = "RECEIVED"
    CONTEXT_PENDING = "CONTEXT_PENDING"
    ANALYSIS_PENDING = "ANALYSIS_PENDING"
    FAILED = "FAILED"


class CandidateSubmission(BaseModel):
    """A contributor's claim of an issue, sourced from one GitHub comment."""

    model_config = ConfigDict(frozen=True)
    candidate_id: str = Field(default_factory=lambda: str(uuid4()))
    repository_owner: str
    repository_name: str
    issue_number: int
    issue_title: str = ""
    issue_body: str = ""
    contributor_username: str
    contributor_github_id: int | None = None
    comment_id: int
    comment_body: str
    approach: str
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: CandidateStatus = CandidateStatus.RECEIVED
    webhook_delivery_id: str
    parent_candidate_id: str | None = None
    priority: int | None = Field(default=None, ge=1)
    analysis_decision: str | None = None
    analysis_result: dict[str, object] | None = None
    is_recommended: bool = False
    is_assigned: bool = False
    assigned_at: datetime | None = None
    unassigned_at: datetime | None = None


class IssueCandidateState(str, Enum):
    WAITING_FOR_CANDIDATES = "WAITING_FOR_CANDIDATES"
    EVALUATING_CANDIDATES = "EVALUATING_CANDIDATES"
    CANDIDATE_RECOMMENDED = "CANDIDATE_RECOMMENDED"
    ASSIGNED = "ASSIGNED"
    RESUME_EVALUATION = "RESUME_EVALUATION"


class IssueEvaluation(BaseModel):
    """Issue-level coordination state for fair, ordered candidate evaluation."""

    model_config = ConfigDict(frozen=True)
    repository_owner: str
    repository_name: str
    issue_number: int
    status: IssueCandidateState = IssueCandidateState.WAITING_FOR_CANDIDATES
    recommended_candidate_id: str | None = None
    assigned_candidate_id: str | None = None
    assigned_username: str | None = None
    next_priority: int = Field(default=1, ge=1)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
