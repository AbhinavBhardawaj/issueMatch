import pytest
from app.domain.states import VerificationStatus, EvidenceStatus
from app.verifier.evidence import EvidenceItemResult
from app.services.issue_gate import should_create_issue, GateDecision

# G1
def test_gate_allow_all_pass(make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_result):
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    dr = make_dedup_result(finding=f)
    
    res = should_create_issue(f, v, er, rc, dr)
    assert res.decision == GateDecision.ALLOW
    assert res.reason == "ALL_CONDITIONS_MET"

# G2
def test_gate_deny_verifier_not_verified(make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_result):
    f = make_finding()
    v = make_verification(finding=f, status=VerificationStatus.REJECTED)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    dr = make_dedup_result(finding=f)
    
    res = should_create_issue(f, v, er, rc, dr)
    assert res.decision == GateDecision.DENY
    assert res.reason == "VERIFIER_NOT_VERIFIED"

# G3
def test_gate_deny_finding_id_mismatch(make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_result):
    f = make_finding()
    v = make_verification(finding=f, finding_id="F-999")
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    dr = make_dedup_result(finding=f)
    
    res = should_create_issue(f, v, er, rc, dr)
    assert res.decision == GateDecision.DENY
    assert res.reason == "FINDING_ID_MISMATCH"

# G4
def test_gate_deny_installation_mismatch(make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_result):
    f = make_finding()
    v = make_verification(finding=f, installation_id="I-999")
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    dr = make_dedup_result(finding=f)
    
    res = should_create_issue(f, v, er, rc, dr)
    assert res.decision == GateDecision.DENY
    assert res.reason == "INSTALLATION_MISMATCH"

# G5
def test_gate_deny_repository_mismatch(make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_result):
    f = make_finding()
    v = make_verification(finding=f, repository_id="R-999")
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    dr = make_dedup_result(finding=f)
    
    res = should_create_issue(f, v, er, rc, dr)
    assert res.decision == GateDecision.DENY
    assert res.reason == "REPOSITORY_MISMATCH"

# G6
def test_gate_deny_commit_sha_mismatch(make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_result):
    f = make_finding()
    v = make_verification(finding=f, commit_sha="C-999")
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    dr = make_dedup_result(finding=f)
    
    res = should_create_issue(f, v, er, rc, dr)
    assert res.decision == GateDecision.DENY
    assert res.reason == "COMMIT_SHA_MISMATCH"

# G7
def test_gate_deny_no_evidence(make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_result):
    f = make_finding(evidence=[])
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    dr = make_dedup_result(finding=f)
    
    res = should_create_issue(f, v, er, rc, dr)
    assert res.decision == GateDecision.DENY
    assert res.reason == "NO_EVIDENCE"

# G8
def test_gate_deny_evidence_not_supported(make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_result):
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f, overall=EvidenceStatus.UNKNOWN)
    rc = make_repo_context()
    dr = make_dedup_result(finding=f)
    
    res = should_create_issue(f, v, er, rc, dr)
    assert res.decision == GateDecision.DENY
    assert res.reason == "EVIDENCE_NOT_SUPPORTED"

# G9
def test_gate_deny_evidence_contradicted(make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_result):
    f = make_finding()
    v = make_verification(finding=f)
    
    # overall is SUPPORTED (somehow bypassed), but an item is CONTRADICTED
    er = make_evidence_result(finding=f, overall=EvidenceStatus.SUPPORTED, results=[
        EvidenceItemResult(
            file="src/auth.py", line=82, snippet="x",
            file_exists=True, line_exists=True, snippet_found=True, function_exists=True,
            status=EvidenceStatus.CONTRADICTED
        )
    ])
    rc = make_repo_context()
    dr = make_dedup_result(finding=f)
    
    res = should_create_issue(f, v, er, rc, dr)
    assert res.decision == GateDecision.DENY
    assert res.reason == "EVIDENCE_CONTRADICTED"

# G10
def test_gate_deny_duplicate(make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_result):
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    dr = make_dedup_result(finding=f, is_duplicate=True)
    
    res = should_create_issue(f, v, er, rc, dr)
    assert res.decision == GateDecision.DENY
    assert res.reason == "DUPLICATE"

# G11
def test_gate_deny_signature_mismatch(make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_result):
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    dr = make_dedup_result(finding=f, signature="wrong_signature")
    
    res = should_create_issue(f, v, er, rc, dr)
    assert res.decision == GateDecision.DENY
    assert res.reason == "SIGNATURE_MISMATCH"

# G12
def test_gate_deny_dedup_reservation_mismatch(make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_result):
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    dr = make_dedup_result(finding=f, finding_id="F-999") # signature matches mock_signature, but finding_id doesn't
    
    res = should_create_issue(f, v, er, rc, dr)
    assert res.decision == GateDecision.DENY
    assert res.reason == "DEDUP_RESERVATION_MISMATCH"
