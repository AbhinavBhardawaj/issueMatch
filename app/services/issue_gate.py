from enum import Enum
from pydantic import BaseModel, ConfigDict
from app.domain.states import VerificationStatus, EvidenceStatus
from app.domain.models import Finding, Verification, RepoContext
from app.verifier.evidence import EvidenceValidationResult
from app.dedup.detector import DedupResult, normalized_finding_signature

class GateDecision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"

class GateResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    
    decision: GateDecision
    reason: str

def should_create_issue(
    finding: Finding, 
    verification: Verification, 
    evidence_result: EvidenceValidationResult, 
    repo_context: RepoContext, 
    dedup_result: DedupResult
) -> GateResult:
    if verification.status != VerificationStatus.VERIFIED:
        return GateResult(decision=GateDecision.DENY, reason="VERIFIER_NOT_VERIFIED")
        
    if verification.finding_id != finding.finding_id:
        return GateResult(decision=GateDecision.DENY, reason="FINDING_ID_MISMATCH")
        
    if verification.installation_id != finding.installation_id:
        return GateResult(decision=GateDecision.DENY, reason="INSTALLATION_MISMATCH")
        
    if verification.repository_id != finding.repository_id:
        return GateResult(decision=GateDecision.DENY, reason="REPOSITORY_MISMATCH")
        
    if verification.commit_sha != finding.commit_sha:
        return GateResult(decision=GateDecision.DENY, reason="COMMIT_SHA_MISMATCH")
        
    if len(finding.evidence) == 0:
        return GateResult(decision=GateDecision.DENY, reason="NO_EVIDENCE")
        
    if evidence_result.overall != EvidenceStatus.SUPPORTED:
        return GateResult(decision=GateDecision.DENY, reason="EVIDENCE_NOT_SUPPORTED")
        
    for item in evidence_result.results:
        if item.status == EvidenceStatus.CONTRADICTED:
            return GateResult(decision=GateDecision.DENY, reason="EVIDENCE_CONTRADICTED")
            
    if dedup_result.is_duplicate:
        return GateResult(decision=GateDecision.DENY, reason="DUPLICATE")
        
    signature = normalized_finding_signature(finding.repository_id, finding.file, finding.function, finding.description)
    if signature != dedup_result.signature:
        return GateResult(decision=GateDecision.DENY, reason="SIGNATURE_MISMATCH")
        
    if dedup_result.finding_id != finding.finding_id:
        return GateResult(decision=GateDecision.DENY, reason="DEDUP_RESERVATION_MISMATCH")
        
    return GateResult(decision=GateDecision.ALLOW, reason="ALL_CONDITIONS_MET")
