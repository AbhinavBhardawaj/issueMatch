from app.domain.states import FindingStatus
from app.domain.models import Finding, Verification

class IllegalTransitionError(Exception):
    pass

class BindingMismatchError(Exception):
    pass

LEGAL_TRANSITIONS: dict[FindingStatus, set[FindingStatus]] = {
    FindingStatus.DISCOVERED: {FindingStatus.VERIFYING},
    FindingStatus.VERIFYING: {FindingStatus.VERIFIED, FindingStatus.REJECTED, FindingStatus.VERIFICATION_FAILED},
    FindingStatus.VERIFIED: {FindingStatus.ISSUE_CREATING},
    FindingStatus.ISSUE_CREATING: {FindingStatus.ISSUE_CREATED},
    FindingStatus.REJECTED: set(),
    FindingStatus.VERIFICATION_FAILED: set(),
    FindingStatus.ISSUE_CREATED: set(),
}

def transition_finding(finding: Finding, new_status: FindingStatus) -> Finding:
    allowed_statuses = LEGAL_TRANSITIONS.get(finding.status, set())
    if new_status not in allowed_statuses:
        raise IllegalTransitionError(f"{finding.status} -> {new_status} is not allowed")
    
    return finding.model_copy(update={"status": new_status})

def assert_binding(finding: Finding, verification: Verification) -> None:
    if verification.finding_id != finding.finding_id:
        raise BindingMismatchError(f"Binding mismatch on finding_id: {verification.finding_id} != {finding.finding_id}")
    
    if verification.installation_id != finding.installation_id:
        raise BindingMismatchError(f"Binding mismatch on installation_id: {verification.installation_id} != {finding.installation_id}")
        
    if verification.repository_id != finding.repository_id:
        raise BindingMismatchError(f"Binding mismatch on repository_id: {verification.repository_id} != {finding.repository_id}")
        
    if verification.commit_sha != finding.commit_sha:
        raise BindingMismatchError(f"Binding mismatch on commit_sha: {verification.commit_sha} != {finding.commit_sha}")
