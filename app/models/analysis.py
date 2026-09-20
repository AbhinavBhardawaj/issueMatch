"""Strict contract implemented by the future analysis package."""
from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .candidate import CandidateSubmission
from .context import RepositoryAnalysisContext


class AnalysisDecision(str, Enum):
    PASS = "PASS"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    REJECT = "REJECT"


class ApproachAnalysis(BaseModel):
    """Validated analysis output that is safe to render as a GitHub response."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    decision: AnalysisDecision
    confidence: float = Field(ge=0, le=1)
    strengths: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    revision_feedback: str = ""
    recommendation: str = ""

    @model_validator(mode="after")
    def validate_decision_content(self) -> "ApproachAnalysis":
        if self.decision is AnalysisDecision.PASS and (not self.evidence or self.confidence <= 0):
            raise ValueError("PASS analyses must include evidence and positive confidence")
        if self.decision is AnalysisDecision.REVISION_REQUIRED and not self.revision_feedback.strip():
            raise ValueError("REVISION_REQUIRED analyses need revision_feedback")
        if self.decision is AnalysisDecision.REJECT and not (any(item.strip() for item in self.issues) or self.recommendation.strip()):
            raise ValueError("REJECT analyses need concrete issues or a recommendation")
        return self


@runtime_checkable
class AnalysisService(Protocol):
    """Boundary owned by Developer B; no AI provider is assumed here."""

    async def analyze(
        self, candidate: CandidateSubmission, context: RepositoryAnalysisContext
    ) -> ApproachAnalysis: ...
