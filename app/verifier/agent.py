import uuid
import re
from typing import Callable
from pydantic import ValidationError
from app.domain.states import VerificationStatus
from app.domain.models import Finding, RepoContext, Verification
from app.verifier.prompts import SYSTEM_PROMPT, build_user_prompt
from app.verifier.schemas import VerifierLLMResponse, LLMInvocationError, MalformedVerifierResponse

def run_verifier(
    finding: Finding, 
    repo_context: RepoContext, 
    llm_caller: Callable[[str, str], str]
) -> Verification:
    
    user_prompt = build_user_prompt(finding, repo_context)
    
    try:
        response_text = llm_caller(SYSTEM_PROMPT, user_prompt)
    except Exception as e:
        raise LLMInvocationError(f"LLM call failed: {str(e)}") from e
        
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
        "REJECTED": VerificationStatus.REJECTED
    }
    
    return Verification(
        verification_id=uuid.uuid4().hex,
        finding_id=finding.finding_id,
        installation_id=finding.installation_id,
        repository_id=finding.repository_id,
        commit_sha=finding.commit_sha,
        status=status_map[llm_response.status],
        reason=llm_response.reason,
        confidence=llm_response.confidence
    )
