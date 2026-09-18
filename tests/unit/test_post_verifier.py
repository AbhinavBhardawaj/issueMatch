import pytest
from app.verifier.deterministic import post_verifier_recheck, RecheckStatus
from app.domain.models import EvidenceItem

# PV1
def test_pv_all_valid(make_finding, make_verification, make_evidence_result, make_repo_context):
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    
    res = post_verifier_recheck(f, v, er, rc)
    assert res.status == RecheckStatus.PASS
    assert res.reason == "ALL_CHECKS_PASSED"

# PV2
def test_pv_wrong_finding_id(make_finding, make_verification, make_evidence_result, make_repo_context):
    f = make_finding()
    v = make_verification(finding=f, finding_id="F-999")
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    
    res = post_verifier_recheck(f, v, er, rc)
    assert res.status == RecheckStatus.FAIL
    assert res.reason == "FINDING_ID_MISMATCH"

# PV3
def test_pv_wrong_installation_id(make_finding, make_verification, make_evidence_result, make_repo_context):
    f = make_finding()
    v = make_verification(finding=f, installation_id="I-999")
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    
    res = post_verifier_recheck(f, v, er, rc)
    assert res.status == RecheckStatus.FAIL
    assert res.reason == "INSTALLATION_MISMATCH"

# PV4
def test_pv_wrong_repository_id(make_finding, make_verification, make_evidence_result, make_repo_context):
    f = make_finding()
    v = make_verification(finding=f, repository_id="R-999")
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    
    res = post_verifier_recheck(f, v, er, rc)
    assert res.status == RecheckStatus.FAIL
    assert res.reason == "REPOSITORY_MISMATCH"

# PV5
def test_pv_wrong_commit_sha(make_finding, make_verification, make_evidence_result, make_repo_context):
    f = make_finding()
    v = make_verification(finding=f, commit_sha="C-999")
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    
    res = post_verifier_recheck(f, v, er, rc)
    assert res.status == RecheckStatus.FAIL
    assert res.reason == "COMMIT_SHA_MISMATCH"

# PV6
def test_pv_evidence_now_contradicted(make_finding, make_verification, make_evidence_result, make_repo_context):
    # finding claims evidence is line 9999 (which is out of bounds for repo_context)
    f = make_finding(evidence=[EvidenceItem(file="src/auth.py", line=9999, snippet="x")])
    v = make_verification(finding=f)
    # the cached evidence result was SUPPORTED (simulating tampering or cache poisoning)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    
    res = post_verifier_recheck(f, v, er, rc)
    assert res.status == RecheckStatus.FAIL
    assert res.reason == "EVIDENCE_CONTRADICTED"

# PV7
def test_pv_repo_context_wrong_commit(make_finding, make_verification, make_evidence_result, make_repo_context):
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context(commit_sha="C-999")
    
    res = post_verifier_recheck(f, v, er, rc)
    assert res.status == RecheckStatus.FAIL
    assert res.reason == "REPO_CONTEXT_COMMIT_SHA_MISMATCH"

# PV8
def test_pv_repo_context_wrong_repo(make_finding, make_verification, make_evidence_result, make_repo_context):
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context(repository_id="R-999")
    
    res = post_verifier_recheck(f, v, er, rc)
    assert res.status == RecheckStatus.FAIL
    assert res.reason == "REPO_CONTEXT_REPOSITORY_MISMATCH"
