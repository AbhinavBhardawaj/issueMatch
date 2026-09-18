from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class VerifierLLMResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    
    status: Literal["VERIFIED", "REJECTED"] = Field(..., description="Either 'VERIFIED' or 'REJECTED'")
    reason: str
    confidence: float = Field(..., ge=0.0, le=1.0)

class LLMInvocationError(Exception):
    pass

class MalformedVerifierResponse(Exception):
    pass
