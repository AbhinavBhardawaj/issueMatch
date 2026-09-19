from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from app.domain.models import VerifierEvidence

class VerifierLLMResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    
    status: Literal["VERIFIED", "REJECTED", "NEEDS_MORE_CONTEXT"] = Field(
        ..., description="Verification status: VERIFIED, REJECTED, or NEEDS_MORE_CONTEXT"
    )
    reason: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    supporting_evidence: list[VerifierEvidence] = Field(default_factory=list)
    counter_evidence: list[VerifierEvidence] = Field(default_factory=list)
    duplicate_issue: bool = False

class LLMInvocationError(Exception):
    pass

class MalformedVerifierResponse(Exception):
    pass
