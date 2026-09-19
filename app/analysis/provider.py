"""Validated transport contract between Strands and repository verification."""
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.models.analysis import AnalysisDecision


class EvidenceCitation(BaseModel):
    """A claim tied to an exact excerpt of source supplied to the model."""

    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=300)
    excerpt: str = Field(min_length=1, max_length=500)
    claim: str = Field(min_length=1, max_length=500)


class AnalysisProviderResult(BaseModel):
    """Schema-constrained model output; evidence is still unverified here."""

    model_config = ConfigDict(extra="forbid")
    decision: AnalysisDecision
    confidence: float = Field(ge=0, le=1)
    strengths: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    evidence: list[EvidenceCitation] = Field(default_factory=list, max_length=10)
    revision_feedback: str = ""
    recommendation: str = ""


class AnalysisProvider(Protocol):
    async def generate(self, prompt: str) -> AnalysisProviderResult:
        """Return validated fields, never arbitrary model text."""
        ...
