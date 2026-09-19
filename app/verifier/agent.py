import logging
import uuid
import re
import time
from typing import Callable, Protocol
from pydantic import ValidationError
from app.domain.states import VerificationStatus
from app.domain.models import Finding, RepoContext, Verification
from app.verifier.prompts import SYSTEM_PROMPT, build_verifier_prompt
from app.verifier.schemas import VerifierLLMResponse, LLMInvocationError, MalformedVerifierResponse

logger = logging.getLogger(__name__)

class LLMProvider(Protocol):
    async def complete(self, system: str, user: str) -> str: ...

async def run_verifier(
    finding: Finding, 
    repo_context: RepoContext, 
    llm_provider: LLMProvider
) -> Verification:
    
    user_prompt = build_verifier_prompt(finding, repo_context)
    
    t0 = time.perf_counter()
    try:
        response_text = await llm_provider.complete(SYSTEM_PROMPT, user_prompt)
    except Exception as e:
        raise LLMInvocationError(f"LLM call failed: {str(e)}") from e
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        
    # Clean up markdown JSON block if present
    response_text = response_text.strip()
    match = re.search(r"```json\s*(.*?)\s*```", response_text, re.DOTALL)
    if match:
        response_text = match.group(1)
        
    try:
        llm_response = VerifierLLMResponse.model_validate_json(response_text)
    except ValidationError as e:
        raise MalformedVerifierResponse(f"Invalid LLM response format: {str(e)}") from e
    except Exception as e:
        raise MalformedVerifierResponse(f"Failed to parse JSON: {str(e)}") from e
        
    status_map = {
        "VERIFIED": VerificationStatus.VERIFIED,
        "REJECTED": VerificationStatus.REJECTED,
        "NEEDS_MORE_CONTEXT": VerificationStatus.NEEDS_MORE_CONTEXT,
    }

    provider_name = getattr(llm_provider, "provider_name", type(llm_provider).__name__)
    model_name = getattr(llm_provider, "model_name", "unknown")
    logger.info(
        "verifier_agent_invoked",
        extra={
            "role": "verifier",
            "provider": provider_name,
            "model": model_name,
            "latency_ms": latency_ms,
            "commit_sha": finding.commit_sha,
            "finding_id": finding.finding_id,
            "result_status": llm_response.status,
        },
    )
    
    return Verification(
        verification_id=uuid.uuid4().hex,
        finding_id=finding.finding_id,
        installation_id=finding.installation_id,
        repository_id=finding.repository_id,
        commit_sha=finding.commit_sha,
        status=status_map[llm_response.status],
        reason=llm_response.reason,
        confidence=llm_response.confidence,
        supporting_evidence=llm_response.supporting_evidence,
        counter_evidence=llm_response.counter_evidence,
        duplicate_issue=llm_response.duplicate_issue,
    )
