from enum import Enum
from pydantic import BaseModel, ConfigDict
from app.domain.states import EvidenceStatus
from app.domain.models import Finding, Verification, RepoContext
from app.verifier.evidence import validate_evidence, EvidenceValidationResult

class RecheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"

class RecheckResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    
    status: RecheckStatus
    reason: str

def post_verifier_recheck(
    finding: Finding, 
    verification: Verification, 
    evidence_result: EvidenceValidationResult, 
    repo_context: RepoContext
) -> RecheckResult:
    
    if str(verification.finding_id) != str(finding.finding_id):
        return RecheckResult(status=RecheckStatus.FAIL, reason="FINDING_ID_MISMATCH")
        
    if str(verification.installation_id) != str(finding.installation_id):
        return RecheckResult(status=RecheckStatus.FAIL, reason="INSTALLATION_MISMATCH")
        
    if str(verification.repository_id) != str(finding.repository_id):
        return RecheckResult(status=RecheckStatus.FAIL, reason="REPOSITORY_MISMATCH")
        
    if str(verification.commit_sha) != str(finding.commit_sha):
        return RecheckResult(status=RecheckStatus.FAIL, reason="COMMIT_SHA_MISMATCH")

    if str(repo_context.installation_id) != str(finding.installation_id):
        return RecheckResult(status=RecheckStatus.FAIL, reason="REPO_CONTEXT_INSTALLATION_MISMATCH")

    if str(repo_context.repository_id) != str(finding.repository_id):
        return RecheckResult(status=RecheckStatus.FAIL, reason="REPO_CONTEXT_REPOSITORY_MISMATCH")
        
    if str(repo_context.commit_sha) != str(finding.commit_sha):
        return RecheckResult(status=RecheckStatus.FAIL, reason="REPO_CONTEXT_COMMIT_SHA_MISMATCH")
        
    fresh_evidence_result = validate_evidence(finding, repo_context)
    
    if fresh_evidence_result.overall == EvidenceStatus.CONTRADICTED:
        return RecheckResult(status=RecheckStatus.FAIL, reason="EVIDENCE_CONTRADICTED")
        
    for item in fresh_evidence_result.results:
        if item.status == EvidenceStatus.CONTRADICTED:
            return RecheckResult(status=RecheckStatus.FAIL, reason="EVIDENCE_ITEM_CONTRADICTED")
            
    return RecheckResult(status=RecheckStatus.PASS, reason="ALL_CHECKS_PASSED")
