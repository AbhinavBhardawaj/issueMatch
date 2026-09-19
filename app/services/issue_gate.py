import hashlib
import json
import re
from datetime import datetime, timezone
import threading
from enum import Enum
from pydantic import BaseModel, ConfigDict
from app.domain.states import VerificationStatus, EvidenceStatus
from app.domain.models import Finding, Verification, RepoContext, ExistingIssue
from app.verifier.evidence import EvidenceValidationResult

# Dedup Logic

class DedupResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    
    signature: str
    finding_id: str
    is_duplicate: bool
    reason: str

STALE_RESERVATION_SECONDS: int = 300

def _normalize_description(description: str) -> str:
    desc = description[:120].lower()
    desc = re.sub(r'[^a-z0-9\s]', '', desc)
    desc = re.sub(r'\s+', ' ', desc).strip()
    return desc

def normalized_finding_signature(repository_id: str, file: str, function: str | None, description: str) -> str:
    normalized = _normalize_description(description)
    payload = json.dumps({
        "repository_id": repository_id, 
        "file": file.strip().lower(), 
        "function": (function or "").strip().lower(), 
        "defect": normalized
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()

class InMemoryDedupStore:
    def __init__(self):
        self._reservations = {}
        self._lock = threading.Lock()
        
    def check_and_reserve(self, finding: Finding) -> DedupResult:
        signature = normalized_finding_signature(
            finding.repository_id, finding.file, finding.function, finding.description
        )
        now = datetime.now(timezone.utc)
        
        with self._lock:
            if signature in self._reservations:
                reservation = self._reservations[signature]
                if reservation["finding_id"] == finding.finding_id:
                    return DedupResult(
                        signature=signature, 
                        finding_id=finding.finding_id, 
                        is_duplicate=False, 
                        reason="Own reservation"
                    )
                
                age_seconds = (now - reservation["timestamp"]).total_seconds()
                if age_seconds > STALE_RESERVATION_SECONDS:
                    self._reservations[signature] = {"finding_id": finding.finding_id, "timestamp": now}
                    return DedupResult(
                        signature=signature, 
                        finding_id=finding.finding_id, 
                        is_duplicate=False, 
                        reason="Overwrote stale reservation"
                    )
                    
                return DedupResult(
                    signature=signature, 
                    finding_id=reservation["finding_id"], 
                    is_duplicate=True, 
                    reason="Active reservation exists"
                )
                
            self._reservations[signature] = {"finding_id": finding.finding_id, "timestamp": now}
            return DedupResult(
                signature=signature, 
                finding_id=finding.finding_id, 
                is_duplicate=False, 
                reason="New reservation"
            )

def check_existing_github_issues(finding: Finding, existing_issues: list[ExistingIssue]) -> bool:
    marker = f"opencontrib:finding:{finding.finding_id}"
    for issue in existing_issues:
        if marker in issue.body_summary:
            return True
    return False

# Gate Logic

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
        
    if dedup_result.finding_id != finding.finding_id:
        return GateResult(decision=GateDecision.DENY, reason="DEDUP_RESERVATION_MISMATCH")
        
    signature = normalized_finding_signature(finding.repository_id, finding.file, finding.function, finding.description)
    if signature != dedup_result.signature:
        return GateResult(decision=GateDecision.DENY, reason="SIGNATURE_MISMATCH")
        
    if repo_context.repository_id != finding.repository_id:
        return GateResult(decision=GateDecision.DENY, reason="REPO_CONTEXT_REPOSITORY_MISMATCH")
        
    if repo_context.installation_id != finding.installation_id:
        return GateResult(decision=GateDecision.DENY, reason="REPO_CONTEXT_INSTALLATION_MISMATCH")
        
    if repo_context.commit_sha != finding.commit_sha:
        return GateResult(decision=GateDecision.DENY, reason="REPO_CONTEXT_COMMIT_SHA_MISMATCH")

    if verification.duplicate_issue or check_existing_github_issues(finding, repo_context.existing_issues):
        return GateResult(decision=GateDecision.DENY, reason="EXISTING_GITHUB_ISSUE")

    # Security findings publication guard: do not publish public GitHub issues for security category
    # Must be held for manual review
    finding_category = getattr(finding, "category", "") or getattr(finding, "severity", "")
    if hasattr(finding, "category") and getattr(finding, "category") == "security":
        return GateResult(decision=GateDecision.DENY, reason="SECURITY_MANUAL_REVIEW_REQUIRED")
        
    return GateResult(decision=GateDecision.ALLOW, reason="ALL_CONDITIONS_MET")
