import pytest
import time
from datetime import datetime, timezone, timedelta
from app.services.issue_gate import (
    InMemoryDedupStore,
    check_existing_github_issues,
    normalized_finding_signature,
    STALE_RESERVATION_SECONDS
)
from app.domain.models import ExistingIssue

# DD1
def test_first_reservation(make_finding):
    store = InMemoryDedupStore()
    f = make_finding()
    res = store.check_and_reserve(f)
    assert not res.is_duplicate
    assert res.reason == "New reservation"

# DD2
def test_same_finding_twice(make_finding):
    store = InMemoryDedupStore()
    f = make_finding()
    res1 = store.check_and_reserve(f)
    assert not res1.is_duplicate
    
    res2 = store.check_and_reserve(f)
    assert not res2.is_duplicate
    assert res2.reason == "Own reservation"

# DD3
def test_different_finding_same_sig(make_finding):
    store = InMemoryDedupStore()
    f1 = make_finding(finding_id="F-1")
    f2 = make_finding(finding_id="F-2") # identical otherwise
    
    res1 = store.check_and_reserve(f1)
    res2 = store.check_and_reserve(f2)
    
    assert not res1.is_duplicate
    assert res2.is_duplicate
    assert res2.reason == "Active reservation exists"
    assert res2.finding_id == "F-1" # Returns the ID of the owner

# DD4
def test_title_change_same_sig(make_finding):
    store = InMemoryDedupStore()
    f1 = make_finding(finding_id="F-1", title="Title A")
    f2 = make_finding(finding_id="F-2", title="Title B") # only title changed
    
    store.check_and_reserve(f1)
    res2 = store.check_and_reserve(f2)
    
    assert res2.is_duplicate

# DD5
def test_different_defect_not_duplicate(make_finding):
    store = InMemoryDedupStore()
    f1 = make_finding(finding_id="F-1", description="Defect A")
    f2 = make_finding(finding_id="F-2", description="Defect B")
    
    res1 = store.check_and_reserve(f1)
    res2 = store.check_and_reserve(f2)
    
    assert not res1.is_duplicate
    assert not res2.is_duplicate

# DD6
def test_stale_reservation_overwritten(make_finding):
    store = InMemoryDedupStore()
    f1 = make_finding(finding_id="F-1")
    f2 = make_finding(finding_id="F-2")
    
    res1 = store.check_and_reserve(f1)
    assert not res1.is_duplicate
    
    # Manually backdate the reservation to be stale
    sig = res1.signature
    store._reservations[sig]["timestamp"] = datetime.now(timezone.utc) - timedelta(seconds=STALE_RESERVATION_SECONDS + 10)
    
    res2 = store.check_and_reserve(f2)
    assert not res2.is_duplicate
    assert res2.reason == "Overwrote stale reservation"
    assert res2.finding_id == "F-2"

# DD7
def test_github_marker_found(make_finding):
    f = make_finding(finding_id="F-123")
    issues = [
        ExistingIssue(number=1, title="Bug", state="open", body_summary="Some issue\n<!-- opencontrib:finding:F-123:v:V-456 -->")
    ]
    assert check_existing_github_issues(f, issues) is True

# DD8
def test_github_marker_not_found(make_finding):
    f = make_finding(finding_id="F-123")
    issues = [
        ExistingIssue(number=1, title="Bug", state="open", body_summary="Some issue\n<!-- opencontrib:finding:F-999:v:V-456 -->")
    ]
    assert check_existing_github_issues(f, issues) is False

# DD9
def test_signature_deterministic():
    sig1 = normalized_finding_signature("repo1", "file.py", "func", "desc")
    sig2 = normalized_finding_signature("repo1", "file.py", "func", "desc")
    assert sig1 == sig2
    
    # Check casing and whitespace are normalized
    sig3 = normalized_finding_signature("repo1", "  FILE.py ", " FUNC ", "  DESC  ")
    assert sig1 == sig3

# DD10
def test_description_length_normalization():
    # Only first 120 chars should matter
    desc1 = "A" * 120 + "B"
    desc2 = "A" * 120 + "C"
    
    sig1 = normalized_finding_signature("repo1", "file.py", "func", desc1)
    sig2 = normalized_finding_signature("repo1", "file.py", "func", desc2)
    
    assert sig1 == sig2

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

import pytest
from app.domain.states import VerificationStatus, EvidenceStatus
from app.verifier.evidence import EvidenceItemResult, EvidenceValidationResult
from app.services.issue_gate import DedupResult, normalized_finding_signature
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
