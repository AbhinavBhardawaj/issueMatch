import pytest
from app.domain.states import FindingStatus, VerificationStatus, EvidenceStatus
from app.domain.models import (
    Finding, Verification, EvidenceItem,
    RepoContext, RepoContextFile, ExistingIssue
)
from app.verifier.evidence import EvidenceItemResult, EvidenceValidationResult
from app.services.issue_gate import GateResult, GateDecision, DedupResult, normalized_finding_signature

@pytest.fixture
def make_finding():
    def _make(**overrides):
        defaults = {
            "finding_id": "F-test001",
            "installation_id": "I-123",
            "repository_id": "R-123",
            "commit_sha": "C-123",
            "title": "Expired JWT returns HTTP 500",
            "severity": "medium",
            "file": "src/auth.py",
            "function": "validate_token",
            "line": 82,
            "description": "JWT expiration is not caught.",
            "expected_behavior": "Should return 401.",
            "evidence": [
                EvidenceItem(
                    file="src/auth.py",
                    line=82,
                    snippet="decoded = jwt.decode(...)"
                )
            ],
            "confidence": 0.9,
            "status": FindingStatus.DISCOVERED
        }
        defaults.update(overrides)
        return Finding(**defaults)
    return _make

@pytest.fixture
def make_verification(make_finding):
    def _make(finding=None, **overrides):
        f = finding or make_finding()
        defaults = {
            "verification_id": "V-test001",
            "finding_id": f.finding_id,
            "installation_id": f.installation_id,
            "repository_id": f.repository_id,
            "commit_sha": f.commit_sha,
            "status": VerificationStatus.VERIFIED,
            "reason": "Looks good",
            "confidence": 0.95
        }
        defaults.update(overrides)
        return Verification(**defaults)
    return _make

@pytest.fixture
def make_repo_context():
    def _make(**overrides):
        auth_content = """import jwt

def validate_token(token):
    try:
        # Some setup
        pass
    except Exception:
        pass
    
    # line 10
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    # line 82
    decoded = jwt.decode(...)
    return decoded
"""
        defaults = {
            "repository_id": "R-123",
            "installation_id": "I-123",
            "owner": "test-owner",
            "name": "test-repo",
            "commit_sha": "C-123",
            "files": [
                RepoContextFile(path="src/auth.py", content=auth_content, size_bytes=len(auth_content))
            ]
        }
        defaults.update(overrides)
        return RepoContext(**defaults)
    return _make

@pytest.fixture
def make_evidence_result(make_finding):
    def _make(finding=None, **overrides):
        f = finding or make_finding()
        defaults = {
            "finding_id": f.finding_id,
            "commit_sha": f.commit_sha,
            "results": [
                EvidenceItemResult(
                    file="src/auth.py",
                    line=82,
                    snippet="decoded = jwt.decode(...)",
                    file_exists=True,
                    line_exists=True,
                    snippet_found=True,
                    function_exists=True,
                    status=EvidenceStatus.SUPPORTED
                )
            ],
            "overall": EvidenceStatus.SUPPORTED
        }
        defaults.update(overrides)
        return EvidenceValidationResult(**defaults)
    return _make

@pytest.fixture
def make_dedup_result(make_finding):
    def _make(finding=None, **overrides):
        f = finding or make_finding()
        
        # Auto-compute the correct deterministic signature for the finding
        sig = normalized_finding_signature(f.repository_id, f.file, f.function, f.description)
        
        defaults = {
            "signature": sig,
            "finding_id": f.finding_id,
            "is_duplicate": False,
            "reason": "No duplicate found"
        }
        defaults.update(overrides)
        return DedupResult(**defaults)
    return _make

@pytest.fixture
def make_gate_result():
    def _make(**overrides):
        defaults = {
            "decision": GateDecision.ALLOW,
            "reason": "ALL_CONDITIONS_MET"
        }
        defaults.update(overrides)
        return GateResult(**defaults)
    return _make
