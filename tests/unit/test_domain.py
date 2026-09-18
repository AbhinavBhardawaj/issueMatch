import pytest
from pydantic import ValidationError
from app.domain.states import FindingStatus, VerificationStatus, EvidenceStatus
from app.domain.models import Finding, Verification, EvidenceItem
from app.domain.transitions import transition_finding, assert_binding, IllegalTransitionError, BindingMismatchError

def make_dummy_finding(status: FindingStatus = FindingStatus.DISCOVERED) -> Finding:
    return Finding(
        finding_id="F-123",
        installation_id="I-123",
        repository_id="R-123",
        commit_sha="C-123",
        title="Test",
        severity="low",
        file="test.py",
        description="Test desc",
        expected_behavior="Test expect",
        confidence=0.5,
        status=status
    )

def make_dummy_verification(
    finding_id="F-123",
    installation_id="I-123",
    repository_id="R-123",
    commit_sha="C-123",
    status=VerificationStatus.PENDING
) -> Verification:
    return Verification(
        verification_id="V-123",
        finding_id=finding_id,
        installation_id=installation_id,
        repository_id=repository_id,
        commit_sha=commit_sha,
        status=status,
        reason="Test",
        confidence=0.5
    )

# D1
def test_discovered_to_verifying():
    f = make_dummy_finding(FindingStatus.DISCOVERED)
    f2 = transition_finding(f, FindingStatus.VERIFYING)
    assert f2.status == FindingStatus.VERIFYING

# D2
def test_verifying_to_verified():
    f = make_dummy_finding(FindingStatus.VERIFYING)
    f2 = transition_finding(f, FindingStatus.VERIFIED)
    assert f2.status == FindingStatus.VERIFIED

# D3
def test_verified_to_issue_creating():
    f = make_dummy_finding(FindingStatus.VERIFIED)
    f2 = transition_finding(f, FindingStatus.ISSUE_CREATING)
    assert f2.status == FindingStatus.ISSUE_CREATING

# D4
def test_issue_creating_to_issue_created():
    f = make_dummy_finding(FindingStatus.ISSUE_CREATING)
    f2 = transition_finding(f, FindingStatus.ISSUE_CREATED)
    assert f2.status == FindingStatus.ISSUE_CREATED

# D5
def test_verifying_to_rejected():
    f = make_dummy_finding(FindingStatus.VERIFYING)
    f2 = transition_finding(f, FindingStatus.REJECTED)
    assert f2.status == FindingStatus.REJECTED

# D6
def test_verifying_to_verification_failed():
    f = make_dummy_finding(FindingStatus.VERIFYING)
    f2 = transition_finding(f, FindingStatus.VERIFICATION_FAILED)
    assert f2.status == FindingStatus.VERIFICATION_FAILED

# D7
def test_discovered_to_issue_created():
    f = make_dummy_finding(FindingStatus.DISCOVERED)
    with pytest.raises(IllegalTransitionError):
        transition_finding(f, FindingStatus.ISSUE_CREATED)

# D8
def test_discovered_to_issue_creating():
    f = make_dummy_finding(FindingStatus.DISCOVERED)
    with pytest.raises(IllegalTransitionError):
        transition_finding(f, FindingStatus.ISSUE_CREATING)

# D9
def test_rejected_to_verified():
    f = make_dummy_finding(FindingStatus.REJECTED)
    with pytest.raises(IllegalTransitionError):
        transition_finding(f, FindingStatus.VERIFIED)

# D10
def test_issue_created_to_discovered():
    f = make_dummy_finding(FindingStatus.ISSUE_CREATED)
    with pytest.raises(IllegalTransitionError):
        transition_finding(f, FindingStatus.DISCOVERED)

# D11
def test_assert_binding_valid():
    f = make_dummy_finding()
    v = make_dummy_verification()
    assert_binding(f, v)

# D12
def test_assert_binding_wrong_finding_id():
    f = make_dummy_finding()
    v = make_dummy_verification(finding_id="F-999")
    with pytest.raises(BindingMismatchError):
        assert_binding(f, v)

# D13
def test_assert_binding_wrong_installation_id():
    f = make_dummy_finding()
    v = make_dummy_verification(installation_id="I-999")
    with pytest.raises(BindingMismatchError):
        assert_binding(f, v)

# D14
def test_assert_binding_wrong_repository_id():
    f = make_dummy_finding()
    v = make_dummy_verification(repository_id="R-999")
    with pytest.raises(BindingMismatchError):
        assert_binding(f, v)

# D15
def test_assert_binding_wrong_commit_sha():
    f = make_dummy_finding()
    v = make_dummy_verification(commit_sha="C-999")
    with pytest.raises(BindingMismatchError):
        assert_binding(f, v)

# D16
def test_finding_is_frozen():
    f = make_dummy_finding()
    with pytest.raises(ValidationError):
        f.status = FindingStatus.VERIFIED
