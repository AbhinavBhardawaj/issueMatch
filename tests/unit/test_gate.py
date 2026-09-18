import pytest
from app.domain.states import VerificationStatus, EvidenceStatus
from app.verifier.evidence import EvidenceItemResult, EvidenceValidationResult
from app.dedup.detector import DedupResult, normalized_finding_signature
from app.services.issue_gate import should_create_issue, GateDecision

# GF1
def test_gate_e2e_allow(make_finding, make_verification, make_evidence_result, make_repo_context):
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    
    # Needs a realistic DedupResult to pass condition 10 (signature check)
    signature = normalized_finding_signature(f.repository_id, f.file, f.function, f.description)
    dr = DedupResult(
        signature=signature,
        finding_id=f.finding_id,
        is_duplicate=False,
        reason="New reservation"
    )
    
    res = should_create_issue(f, v, er, rc, dr)
    assert res.decision == GateDecision.ALLOW
    assert res.reason == "ALL_CONDITIONS_MET"

# GF2
def test_gate_e2e_contradicted_evidence(make_finding, make_verification, make_repo_context):
    f = make_finding()
    v = make_verification(finding=f)
    rc = make_repo_context()
    
    er = EvidenceValidationResult(
        finding_id=f.finding_id,
        commit_sha=f.commit_sha,
        results=[
            EvidenceItemResult(
                file=f.file, line=f.line, snippet="foo",
                file_exists=True, line_exists=True, snippet_found=False, function_exists=None,
                status=EvidenceStatus.CONTRADICTED
            )
        ],
        overall=EvidenceStatus.CONTRADICTED
    )
    
    signature = normalized_finding_signature(f.repository_id, f.file, f.function, f.description)
    dr = DedupResult(signature=signature, finding_id=f.finding_id, is_duplicate=False, reason="New")
    
    res = should_create_issue(f, v, er, rc, dr)
    assert res.decision == GateDecision.DENY
    assert res.reason == "EVIDENCE_NOT_SUPPORTED"

# GF3
def test_gate_e2e_duplicate(make_finding, make_verification, make_evidence_result, make_repo_context):
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    
    signature = normalized_finding_signature(f.repository_id, f.file, f.function, f.description)
    dr = DedupResult(
        signature=signature,
        finding_id=f.finding_id,
        is_duplicate=True, # Flagged as duplicate
        reason="Active reservation exists"
    )
    
    res = should_create_issue(f, v, er, rc, dr)
    assert res.decision == GateDecision.DENY
    assert res.reason == "DUPLICATE"
