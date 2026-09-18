"""Candidate-submission domain types."""
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class CandidateStatus(str, Enum):
    RECEIVED = "RECEIVED"
    CONTEXT_PENDING = "CONTEXT_PENDING"
    ANALYSIS_PENDING = "ANALYSIS_PENDING"
    ANALYZED = "ANALYZED"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    REJECTED = "REJECTED"
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
