import asyncio
import hashlib
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.domain.models import Finding, Verification, EvidenceItem, RepoContext, RepoContextFile
from app.domain.states import FindingStatus, VerificationStatus, EvidenceStatus
from app.verifier.evidence import EvidenceItemResult, EvidenceValidationResult
from app.services.issue_gate import (
    GateResult,
    GateDecision,
    DedupResult,
    InMemoryDedupStore,
    compute_defect_signature,
    compute_finding_signature,
    normalized_finding_signature,
    evaluate_issue_authorization,
    should_create_issue,
)
from app.services.issue_creator import (
    create_issue_if_authorized,
    find_existing_issue,
    Unauthorized,
    IssueCreationResult,
    IssueCreationError,
    ReconciliationUnavailableError,
)
from app.github.client import (
    GitHubAmbiguousWriteError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubAuthenticationError,
    GitHubServerError,
    GitHubRequestTimeoutError,
)
from app.services.pipeline import VerifierPipeline
from app.scout.agent import ScoutAgent


# ===========================================================================
# 1. VERIFIER PIPELINE WRITE CLIENT ISOLATION
# ===========================================================================

def test_pipeline_constructor_has_no_github_client():
    """VerifierPipeline must not accept or store a github_write_client attribute."""
    mock_llm = MagicMock()
    pipeline = VerifierPipeline(llm_provider=mock_llm)

    assert not hasattr(pipeline, "github_write_client")
    assert not hasattr(pipeline, "write_client")
    assert not hasattr(pipeline, "client")


@pytest.mark.asyncio
async def test_pipeline_run_requires_github_write_client(make_finding, make_repo_context):
    """Calling pipeline.run without github_write_client must raise TypeError immediately."""
    mock_llm = MagicMock()
    pipeline = VerifierPipeline(llm_provider=mock_llm)
    f = make_finding()
    rc = make_repo_context()

    # Attempt calling run without keyword-only github_write_client
    with pytest.raises(TypeError):
        await pipeline.run(f, rc, "owner", "repo")


# ===========================================================================
# 2. DEFECT SIGNATURE: WORDING DRIFT & PROCESS RESTART
# ===========================================================================

def test_signature_resilient_to_description_wording_drift(make_finding):
    """
    Two findings describing the same defect with different words must yield
    the exact same deterministic signature.
    """
    f1 = make_finding(
        description="division fails when list is empty",
        category="behavioral_bug",
        expected_behavior="Handle empty collections without crashing",
        evidence=[EvidenceItem(file="app/calc.py", line=10, snippet="return total / len(items)")],
    )
    f2 = make_finding(
        description="empty collections cause a zero divisor crash",
        category="behavioral_bug",
        expected_behavior="Handle empty collections without crashing",
        evidence=[EvidenceItem(file="app/calc.py", line=10, snippet="return total / len(items)")],
    )

    sig1 = compute_finding_signature(f1)
    sig2 = compute_finding_signature(f2)

    assert sig1 == sig2, f"Signature differed despite same defect semantics: {sig1} != {sig2}"


def test_signature_resilient_to_line_shift_and_whitespace(make_finding):
    """Signature must be resilient to nearby line shifts and minor code whitespace changes."""
    f1 = make_finding(
        line=10,
        evidence=[EvidenceItem(file="app/calc.py", line=10, snippet="  return total / len(items)  \n")],
    )
    f2 = make_finding(
        line=15,  # line shifted
        evidence=[EvidenceItem(file="app/calc.py", line=15, snippet="return total / len(items)")],
    )

    sig1 = compute_finding_signature(f1)
    sig2 = compute_finding_signature(f2)

    assert sig1 == sig2


@pytest.mark.asyncio
async def test_process_restart_duplicate_recovery_via_signature(make_finding, make_verification, make_dedup_store):
    """
    Process restarts with fresh DedupStore, new finding ID, new verification ID.
    Existing issue on GitHub with matching signature marker must be recovered.
    create_issue must NOT be called.
    """
    f = make_finding(finding_id="F-NEW-123")
    v = make_verification(finding=f, verification_id="V-NEW-456")
    sig = compute_finding_signature(f)

    store = make_dedup_store
    dedup = store.check_and_reserve(f)

    client = AsyncMock()
    client.get_repository.return_value = {"id": f.repository_id, "name": "repo"}
    # Search finds existing issue containing signature marker
    client.search_issues.return_value = [
        {"number": 999, "body": f"Problem description\n<!-- opencontrib:signature:{sig} -->"}
    ]

    er = EvidenceValidationResult(
        finding_id=f.finding_id,
        commit_sha=f.commit_sha,
        results=[EvidenceItemResult(
            file=f.file, line=f.line, snippet=f.evidence[0].snippet,
            file_exists=True, line_exists=True, snippet_found=True, function_exists=True,
            status=EvidenceStatus.SUPPORTED
        )],
        overall=EvidenceStatus.SUPPORTED,
    )
    rc = RepoContext(
        repository_id=f.repository_id,
        installation_id=f.installation_id,
        owner="owner",
        name="repo",
        commit_sha=f.commit_sha,
    )
    gate = GateResult(decision=GateDecision.ALLOW, reason="ALL_CONDITIONS_MET")

    result = await create_issue_if_authorized(
        gate, dedup, f, v, client, "owner", "repo",
        evidence_result=er, repo_context=rc, dedup_store=store
    )

    assert result.was_existing is True
    assert result.issue_number == 999
    client.create_issue.assert_not_called()


# ===========================================================================
# 3. SECURITY GATE-FORGERY ATTACK
# ===========================================================================

@pytest.mark.asyncio
async def test_security_gate_forgery_rejected(make_finding, make_verification, make_repo_context, make_evidence_result, make_dedup_store):
    """
    Finding: category='security', severity='critical', valid evidence
    Verification: VERIFIED, all IDs match
    RepoContext: matches
    Evidence: SUPPORTED
    Caller passes forged GateResult(ALLOW)
    Expected: IssueCreator independently rejects with Unauthorized; create_issue NOT called.
    """
    f = make_finding(category="security", severity="critical")
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    store = make_dedup_store
    dedup = store.check_and_reserve(f)

    client = AsyncMock()
    client.get_repository.return_value = {"id": f.repository_id, "name": "test-repo"}
    forged_gate = GateResult(decision=GateDecision.ALLOW, reason="FORGED_BYPASS")

    with pytest.raises(Unauthorized, match="SECURITY_MANUAL_REVIEW_REQUIRED"):
        await create_issue_if_authorized(
            forged_gate, dedup, f, v, client, "test-owner", "test-repo",
            evidence_result=er, repo_context=rc, dedup_store=store
        )

    client.create_issue.assert_not_called()


# ===========================================================================
# 4. BINDING FORGERY ATTACKS MATRIX (A through J)
# ===========================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,mutation", [
    ("A_verification_repo_mismatch", lambda f, v, er, rc, dr: (f, v.model_copy(update={"repository_id": "WRONG"}), er, rc, dr)),
    ("B_verification_installation_mismatch", lambda f, v, er, rc, dr: (f, v.model_copy(update={"installation_id": "WRONG"}), er, rc, dr)),
    ("C_verification_commit_mismatch", lambda f, v, er, rc, dr: (f, v.model_copy(update={"commit_sha": "WRONG"}), er, rc, dr)),
    ("D_repo_context_repo_mismatch", lambda f, v, er, rc, dr: (f, v, er, rc.model_copy(update={"repository_id": "WRONG"}), dr)),
    ("E_repo_context_installation_mismatch", lambda f, v, er, rc, dr: (f, v, er, rc.model_copy(update={"installation_id": "WRONG"}), dr)),
    ("F_repo_context_commit_mismatch", lambda f, v, er, rc, dr: (f, v, er, rc.model_copy(update={"commit_sha": "WRONG"}), dr)),
    ("G_evidence_unknown", lambda f, v, er, rc, dr: (f, v, er.model_copy(update={"overall": EvidenceStatus.UNKNOWN}), rc, dr)),
    ("H_evidence_contradicted", lambda f, v, er, rc, dr: (f, v, er.model_copy(update={"overall": EvidenceStatus.CONTRADICTED}), rc, dr)),
    ("I_forged_dedup_signature", lambda f, v, er, rc, dr: (f, v, er, rc, dr.model_copy(update={"signature": "forged_sig"}))),
    ("J_dedup_reservation_different_finding", lambda f, v, er, rc, dr: (f, v, er, rc, dr.model_copy(update={"finding_id": "OTHER-FINDING"}))),
])
async def test_binding_forgery_attacks_rejected(
    scenario, mutation, make_finding, make_verification, make_repo_context, make_evidence_result, make_dedup_store
):
    """All binding forgery attacks with forged GateResult(ALLOW) must fail closed with Unauthorized."""
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    store = make_dedup_store
    dr = store.check_and_reserve(f)

    # Apply mutation
    f, v, er, rc, dr = mutation(f, v, er, rc, dr)

    client = AsyncMock()
    client.get_repository.return_value = {"id": f.repository_id, "name": rc.name}
    client.search_issues.return_value = []
    forged_gate = GateResult(decision=GateDecision.ALLOW, reason="FORGED")

    with pytest.raises(Unauthorized):
        await create_issue_if_authorized(
            forged_gate, dr, f, v, client, "test-owner", "test-repo",
            evidence_result=er, repo_context=rc, dedup_store=store
        )

    client.create_issue.assert_not_called()


# ===========================================================================
# 5. GITHUB SEARCH INDEX DELAY (FALLBACK TO RECENT ISSUES)
# ===========================================================================

@pytest.mark.asyncio
async def test_search_index_delay_finds_issue_via_recent_listing(make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_store):
    """
    When GitHub search index is delayed and returns [], the fallback recent issue
    inspection recovers the existing issue with exact signature marker.
    """
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    sig = compute_finding_signature(f)
    store = make_dedup_store
    dr = store.check_and_reserve(f)

    client = AsyncMock()
    client.get_repository.return_value = {"id": f.repository_id, "name": "test-repo"}
    # Search index delayed -> returns []
    client.search_issues.return_value = []
    # Recent issues list contains the issue
    client.get_issues.return_value = [
        {"number": 314, "body": f"Bug details\n<!-- opencontrib:signature:{sig} -->"}
    ]

    gate = GateResult(decision=GateDecision.ALLOW, reason="ALL_CONDITIONS_MET")
    result = await create_issue_if_authorized(
        gate, dr, f, v, client, "test-owner", "test-repo",
        evidence_result=er, repo_context=rc, dedup_store=store
    )

    assert result.was_existing is True
    assert result.issue_number == 314
    client.create_issue.assert_not_called()


# ===========================================================================
# 6. REAL AMBIGUOUS WRITE TIMEOUT BRANCH
# ===========================================================================

@pytest.mark.asyncio
async def test_real_ambiguous_write_timeout_reconciles_without_second_post(
    make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_store
):
    """
    Reconcile search #1: []
    Recent-issues fallback #1: []
    create_issue #1: raises GitHubAmbiguousWriteError
    Reconcile search #2: [issue containing signature marker]
    Expected:
        create_issue.call_count == 1
        result.was_existing == True
        result.issue_number == expected
        NO second create_issue call.
    """
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    sig = compute_finding_signature(f)
    store = make_dedup_store
    dr = store.check_and_reserve(f)

    call_seq = {"search_count": 0, "create_count": 0}

    async def fake_search(owner, repo, query):
        call_seq["search_count"] += 1
        if call_seq["create_count"] == 0:
            return []
        # After create_issue failed, search returns the created issue
        return [{"number": 555, "body": f"Problem\n<!-- opencontrib:signature:{sig} -->"}]

    async def fake_create(owner, repo, title, body, labels=None):
        call_seq["create_count"] += 1
        raise GitHubAmbiguousWriteError("Timeout waiting for response; issue may exist")

    client = AsyncMock()
    client.get_repository.return_value = {"id": f.repository_id, "name": "test-repo"}
    client.search_issues.side_effect = fake_search
    client.get_issues.return_value = []
    client.create_issue.side_effect = fake_create

    gate = GateResult(decision=GateDecision.ALLOW, reason="ALL_CONDITIONS_MET")
    result = await create_issue_if_authorized(
        gate, dr, f, v, client, "test-owner", "test-repo",
        evidence_result=er, repo_context=rc, dedup_store=store
    )

    assert result.was_existing is True
    assert result.issue_number == 555
    assert client.create_issue.call_count == 1, "Must not retry POST after ambiguous write issue was discovered"


# ===========================================================================
# 7. RETRY SAFETY ATTACK: ISSUE APPEARS IMMEDIATELY BEFORE RETRY
# ===========================================================================

@pytest.mark.asyncio
async def test_retry_safety_reconciliation_before_retry_prevents_second_post(
    make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_store
):
    """
    search #1 -> []
    list recent #1 -> []
    create_issue #1 -> ambiguous timeout
    search #2 -> []
    list recent #2 -> []
    before retry: search #3 -> issue appears!
    Expected: NO second POST. create_issue.call_count == 1.
    """
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    sig = compute_finding_signature(f)
    store = make_dedup_store
    dr = store.check_and_reserve(f)

    search_calls = 0

    async def dynamic_search(owner, repo, query):
        nonlocal search_calls
        search_calls += 1
        # Search 1: before create_issue -> []
        # Search 2: immediately after ambiguous timeout -> []
        # Search 3: before retry attempt -> appears!
        if search_calls >= 3:
            return [{"number": 888, "body": f"<!-- opencontrib:signature:{sig} -->"}]
        return []

    client = AsyncMock()
    client.get_repository.return_value = {"id": f.repository_id, "name": "test-repo"}
    client.search_issues.side_effect = dynamic_search
    client.get_issues.return_value = []
    client.create_issue.side_effect = GitHubAmbiguousWriteError("Network timeout")

    gate = GateResult(decision=GateDecision.ALLOW, reason="ALL_CONDITIONS_MET")
    result = await create_issue_if_authorized(
        gate, dr, f, v, client, "test-owner", "test-repo",
        evidence_result=er, repo_context=rc, dedup_store=store
    )

    assert result.was_existing is True
    assert result.issue_number == 888
    assert client.create_issue.call_count == 1


# ===========================================================================
# 8. VALID ISSUE CREATION (CONTROL CASE)
# ===========================================================================

@pytest.mark.asyncio
async def test_valid_issue_creation_exactly_one_call(
    make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_store
):
    """Valid authorized finding results in exactly one create_issue call."""
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    sig = compute_finding_signature(f)
    store = make_dedup_store
    dr = store.check_and_reserve(f)

    client = AsyncMock()
    client.get_repository.return_value = {"id": f.repository_id, "name": "test-repo"}
    client.search_issues.return_value = []
    client.get_issues.return_value = []
    client.create_issue.return_value = {"number": 1001, "body": "created"}

    gate = GateResult(decision=GateDecision.ALLOW, reason="ALL_CONDITIONS_MET")
    result = await create_issue_if_authorized(
        gate, dr, f, v, client, "test-owner", "test-repo",
        evidence_result=er, repo_context=rc, dedup_store=store
    )

    assert result.was_existing is False
    assert result.issue_number == 1001
    assert client.create_issue.call_count == 1


# ===========================================================================
# 9. MANDATORY REAL EVIDENCE AND REPO CONTEXT BOUNDARY ATTACKS
# ===========================================================================

@pytest.mark.asyncio
async def test_issue_creator_rejects_omitted_evidence_result(
    make_finding, make_verification, make_repo_context, make_dedup_store
):
    """Omitting evidence_result must fail immediately; create_issue never called."""
    f = make_finding()
    v = make_verification(finding=f)
    rc = make_repo_context()
    store = make_dedup_store
    dr = store.check_and_reserve(f)

    client = AsyncMock()
    client.get_repository.return_value = {"id": f.repository_id, "name": "test-repo"}
    forged_gate = GateResult(decision=GateDecision.ALLOW, reason="FORGED")

    # A) Omit keyword argument entirely
    with pytest.raises((TypeError, Unauthorized)):
        await create_issue_if_authorized(
            forged_gate, dr, f, v, client, "test-owner", "test-repo",
            repo_context=rc, dedup_store=store  # evidence_result omitted
        )
    client.create_issue.assert_not_called()

    # B) Pass None explicitly
    with pytest.raises(Unauthorized):
        await create_issue_if_authorized(
            forged_gate, dr, f, v, client, "test-owner", "test-repo",
            evidence_result=None,
            repo_context=rc,
            dedup_store=store,
        )
    client.create_issue.assert_not_called()


@pytest.mark.asyncio
async def test_issue_creator_rejects_omitted_repo_context(
    make_finding, make_verification, make_evidence_result, make_dedup_store
):
    """Omitting repo_context must fail immediately; create_issue never called."""
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    store = make_dedup_store
    dr = store.check_and_reserve(f)

    client = AsyncMock()
    client.get_repository.return_value = {"id": f.repository_id, "name": "test-repo"}
    forged_gate = GateResult(decision=GateDecision.ALLOW, reason="FORGED")

    # A) Omit keyword argument entirely
    with pytest.raises((TypeError, Unauthorized)):
        await create_issue_if_authorized(
            forged_gate, dr, f, v, client, "test-owner", "test-repo",
            evidence_result=er, dedup_store=store  # repo_context omitted
        )
    client.create_issue.assert_not_called()

    # B) Pass None explicitly
    with pytest.raises(Unauthorized):
        await create_issue_if_authorized(
            forged_gate, dr, f, v, client, "test-owner", "test-repo",
            evidence_result=er,
            repo_context=None,
            dedup_store=store,
        )
    client.create_issue.assert_not_called()


@pytest.mark.asyncio
async def test_issue_creator_rejects_omitted_dedup_store(
    make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_store
):
    """Omitting dedup_store must fail immediately; create_issue never called."""
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    store = make_dedup_store
    dr = store.check_and_reserve(f)

    client = AsyncMock()
    client.get_repository.return_value = {"id": f.repository_id, "name": "test-repo"}
    forged_gate = GateResult(decision=GateDecision.ALLOW, reason="FORGED")

    # A) Omit keyword argument entirely
    with pytest.raises((TypeError, Unauthorized)):
        await create_issue_if_authorized(
            forged_gate, dr, f, v, client, "test-owner", "test-repo",
            evidence_result=er, repo_context=rc  # dedup_store omitted
        )
    client.create_issue.assert_not_called()

    # B) Pass None explicitly
    with pytest.raises(Unauthorized):
        await create_issue_if_authorized(
            forged_gate, dr, f, v, client, "test-owner", "test-repo",
            evidence_result=er,
            repo_context=rc,
            dedup_store=None,
        )
    client.create_issue.assert_not_called()


# ===========================================================================
# 10. RECONCILIATION OPERATIONAL FAILURE: FAIL CLOSED
# ===========================================================================

@pytest.mark.asyncio
async def test_reconciliation_failure_both_reads_timeout_fails_closed(
    make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_store
):
    """If both search_issues and get_issues time out, do NOT proceed to create_issue."""
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    store = make_dedup_store
    dr = store.check_and_reserve(f)

    client = AsyncMock()
    client.get_repository.return_value = {"id": f.repository_id, "name": "test-repo"}
    client.search_issues.side_effect = GitHubRequestTimeoutError("search timeout")
    client.get_issues.side_effect = GitHubRequestTimeoutError("get_issues timeout")

    gate = GateResult(decision=GateDecision.ALLOW, reason="ALL_CONDITIONS_MET")
    with pytest.raises((ReconciliationUnavailableError, IssueCreationError)):
        await create_issue_if_authorized(
            gate, dr, f, v, client, "test-owner", "test-repo",
            evidence_result=er, repo_context=rc, dedup_store=store
        )

    client.create_issue.assert_not_called()


@pytest.mark.asyncio
async def test_reconciliation_search_fails_but_recent_listing_succeeds(
    make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_store
):
    """If search fails but recent listing succeeds with empty list, normal POST may continue."""
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    store = make_dedup_store
    dr = store.check_and_reserve(f)

    client = AsyncMock()
    client.get_repository.return_value = {"id": f.repository_id, "name": "test-repo"}
    client.search_issues.side_effect = GitHubRequestTimeoutError("search timeout")
    client.get_issues.return_value = []
    client.create_issue.return_value = {"number": 1002, "body": "created"}

    gate = GateResult(decision=GateDecision.ALLOW, reason="ALL_CONDITIONS_MET")
    result = await create_issue_if_authorized(
        gate, dr, f, v, client, "test-owner", "test-repo",
        evidence_result=er, repo_context=rc, dedup_store=store
    )

    assert result.was_existing is False
    assert result.issue_number == 1002
    assert client.create_issue.call_count == 1


# ===========================================================================
# 11. LEGACY SIGNATURE REMOVED FROM WRITE AUTHORIZATION
# ===========================================================================

@pytest.mark.asyncio
async def test_legacy_signature_rejected_by_write_authorization(
    make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_store
):
    """A legacy description-based signature cannot authorize a new write."""
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    store = make_dedup_store

    # Create a valid legacy description signature that differs from compute_finding_signature
    legacy_sig = normalized_finding_signature(f.repository_id, f.file, f.function, f.description)
    dr = DedupResult(signature=legacy_sig, finding_id=f.finding_id, is_duplicate=False, reason="ok")

    # Check evaluate_issue_authorization directly
    gate_decision = evaluate_issue_authorization(f, v, er, rc, dr)
    assert gate_decision.decision == GateDecision.DENY
    assert gate_decision.reason == "SIGNATURE_MISMATCH"

    # Check direct IssueCreator boundary
    client = AsyncMock()
    client.get_repository.return_value = {"id": f.repository_id, "name": "test-repo"}
    forged_gate = GateResult(decision=GateDecision.ALLOW, reason="FORGED")
    with pytest.raises(Unauthorized):
        await create_issue_if_authorized(
            forged_gate, dr, f, v, client, "test-owner", "test-repo",
            evidence_result=er, repo_context=rc, dedup_store=store
        )

    client.create_issue.assert_not_called()


# ===========================================================================
# 12. EVIDENCE RESULT TO FINDING BINDING ATTACKS (Section 1)
# ===========================================================================

@pytest.mark.asyncio
async def test_evidence_result_finding_id_mismatch_denied(
    make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_store
):
    """Attack A: wrong evidence_result.finding_id must be DENIED and rejected at write boundary."""
    f = make_finding(finding_id="F-111")
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f, finding_id="F-222")
    rc = make_repo_context()
    store = make_dedup_store
    dr = store.check_and_reserve(f)

    decision = evaluate_issue_authorization(f, v, er, rc, dr)
    assert decision.decision == GateDecision.DENY
    assert decision.reason == "EVIDENCE_FINDING_ID_MISMATCH"

    client = AsyncMock()
    client.get_repository.return_value = {"id": f.repository_id, "name": "test-repo"}
    forged_gate = GateResult(decision=GateDecision.ALLOW, reason="FORGED")
    with pytest.raises(Unauthorized):
        await create_issue_if_authorized(
            forged_gate, dr, f, v, client, "test-owner", "test-repo",
            evidence_result=er, repo_context=rc, dedup_store=store
        )
    client.create_issue.assert_not_called()


@pytest.mark.asyncio
async def test_evidence_result_commit_sha_mismatch_denied(
    make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_store
):
    """Attack B: wrong evidence_result.commit_sha must be DENIED and rejected at write boundary."""
    f = make_finding(commit_sha="commit-aaa")
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f, commit_sha="commit-bbb")
    rc = make_repo_context(commit_sha="commit-aaa")
    store = make_dedup_store
    dr = store.check_and_reserve(f)

    decision = evaluate_issue_authorization(f, v, er, rc, dr)
    assert decision.decision == GateDecision.DENY
    assert decision.reason == "EVIDENCE_COMMIT_SHA_MISMATCH"

    client = AsyncMock()
    client.get_repository.return_value = {"id": f.repository_id, "name": "test-repo"}
    forged_gate = GateResult(decision=GateDecision.ALLOW, reason="FORGED")
    with pytest.raises(Unauthorized):
        await create_issue_if_authorized(
            forged_gate, dr, f, v, client, "test-owner", "test-repo",
            evidence_result=er, repo_context=rc, dedup_store=store
        )
    client.create_issue.assert_not_called()


@pytest.mark.asyncio
async def test_evidence_result_supported_with_zero_results_denied(
    make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_store
):
    """Attack C: overall=SUPPORTED with results=[] when finding has evidence must NEVER authorize."""
    f = make_finding(evidence=[EvidenceItem(file="src/auth.py", line=82, snippet="x = 1")])
    v = make_verification(finding=f)
    er = EvidenceValidationResult(
        finding_id=f.finding_id,
        commit_sha=f.commit_sha,
        results=[],
        overall=EvidenceStatus.SUPPORTED,
    )
    rc = make_repo_context()
    store = make_dedup_store
    dr = store.check_and_reserve(f)

    decision = evaluate_issue_authorization(f, v, er, rc, dr)
    assert decision.decision == GateDecision.DENY
    assert decision.reason == "EVIDENCE_COVERAGE_MISMATCH"

    client = AsyncMock()
    client.get_repository.return_value = {"id": f.repository_id, "name": "test-repo"}
    forged_gate = GateResult(decision=GateDecision.ALLOW, reason="FORGED")
    with pytest.raises(Unauthorized):
        await create_issue_if_authorized(
            forged_gate, dr, f, v, client, "test-owner", "test-repo",
            evidence_result=er, repo_context=rc, dedup_store=store
        )
    client.create_issue.assert_not_called()


@pytest.mark.asyncio
async def test_evidence_result_substituted_file_denied(
    make_finding, make_verification, make_repo_context, make_dedup_store
):
    """Attack D: Finding cites file A but evidence result validates file B."""
    f = make_finding(evidence=[EvidenceItem(file="src/auth.py", line=82, snippet="x = 1")])
    v = make_verification(finding=f)
    er = EvidenceValidationResult(
        finding_id=f.finding_id,
        commit_sha=f.commit_sha,
        results=[
            EvidenceItemResult(
                file="src/other.py",
                line=82,
                snippet="x = 1",
                file_exists=True,
                line_exists=True,
                snippet_found=True,
                function_exists=True,
                status=EvidenceStatus.SUPPORTED,
            )
        ],
        overall=EvidenceStatus.SUPPORTED,
    )
    rc = make_repo_context()
    store = make_dedup_store
    dr = store.check_and_reserve(f)

    decision = evaluate_issue_authorization(f, v, er, rc, dr)
    assert decision.decision == GateDecision.DENY
    assert decision.reason == "EVIDENCE_COVERAGE_MISMATCH"

    client = AsyncMock()
    client.get_repository.return_value = {"id": f.repository_id, "name": "test-repo"}
    forged_gate = GateResult(decision=GateDecision.ALLOW, reason="FORGED")
    with pytest.raises(Unauthorized):
        await create_issue_if_authorized(
            forged_gate, dr, f, v, client, "test-owner", "test-repo",
            evidence_result=er, repo_context=rc, dedup_store=store
        )
    client.create_issue.assert_not_called()


@pytest.mark.asyncio
async def test_evidence_result_substituted_line_denied(
    make_finding, make_verification, make_repo_context, make_dedup_store
):
    """Attack E: Finding cites line 25 but evidence result validates line 30."""
    f = make_finding(evidence=[EvidenceItem(file="src/auth.py", line=25, snippet="x = 1")])
    v = make_verification(finding=f)
    er = EvidenceValidationResult(
        finding_id=f.finding_id,
        commit_sha=f.commit_sha,
        results=[
            EvidenceItemResult(
                file="src/auth.py",
                line=30,
                snippet="x = 1",
                file_exists=True,
                line_exists=True,
                snippet_found=True,
                function_exists=True,
                status=EvidenceStatus.SUPPORTED,
            )
        ],
        overall=EvidenceStatus.SUPPORTED,
    )
    rc = make_repo_context()
    store = make_dedup_store
    dr = store.check_and_reserve(f)

    decision = evaluate_issue_authorization(f, v, er, rc, dr)
    assert decision.decision == GateDecision.DENY
    assert decision.reason == "EVIDENCE_COVERAGE_MISMATCH"

    client = AsyncMock()
    client.get_repository.return_value = {"id": f.repository_id, "name": "test-repo"}
    forged_gate = GateResult(decision=GateDecision.ALLOW, reason="FORGED")
    with pytest.raises(Unauthorized):
        await create_issue_if_authorized(
            forged_gate, dr, f, v, client, "test-owner", "test-repo",
            evidence_result=er, repo_context=rc, dedup_store=store
        )
    client.create_issue.assert_not_called()


@pytest.mark.asyncio
async def test_evidence_result_substituted_snippet_denied(
    make_finding, make_verification, make_repo_context, make_dedup_store
):
    """Attack F: Finding snippet differs from validated snippet."""
    f = make_finding(evidence=[EvidenceItem(file="src/auth.py", line=82, snippet="original snippet")])
    v = make_verification(finding=f)
    er = EvidenceValidationResult(
        finding_id=f.finding_id,
        commit_sha=f.commit_sha,
        results=[
            EvidenceItemResult(
                file="src/auth.py",
                line=82,
                snippet="different snippet substituted",
                file_exists=True,
                line_exists=True,
                snippet_found=True,
                function_exists=True,
                status=EvidenceStatus.SUPPORTED,
            )
        ],
        overall=EvidenceStatus.SUPPORTED,
    )
    rc = make_repo_context()
    store = make_dedup_store
    dr = store.check_and_reserve(f)

    decision = evaluate_issue_authorization(f, v, er, rc, dr)
    assert decision.decision == GateDecision.DENY
    assert decision.reason == "EVIDENCE_COVERAGE_MISMATCH"

    client = AsyncMock()
    client.get_repository.return_value = {"id": f.repository_id, "name": "test-repo"}
    forged_gate = GateResult(decision=GateDecision.ALLOW, reason="FORGED")
    with pytest.raises(Unauthorized):
        await create_issue_if_authorized(
            forged_gate, dr, f, v, client, "test-owner", "test-repo",
            evidence_result=er, repo_context=rc, dedup_store=store
        )
    client.create_issue.assert_not_called()


# ===========================================================================
# 13. WRITE DESTINATION BINDING ATTACKS (Section 2)
# ===========================================================================

@pytest.mark.asyncio
async def test_write_destination_owner_mismatch_rejected(
    make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_store
):
    """Attack A1: RepoContext has owner org-A, but create_issue_if_authorized called with org-B."""
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context(owner="org-A", name="repo-A")
    store = make_dedup_store
    dr = store.check_and_reserve(f)

    client = AsyncMock()
    gate = GateResult(decision=GateDecision.ALLOW, reason="ALL_CONDITIONS_MET")

    with pytest.raises(Unauthorized):
        await create_issue_if_authorized(
            gate, dr, f, v, client, "org-B", "repo-A",
            evidence_result=er, repo_context=rc, dedup_store=store
        )
    client.create_issue.assert_not_called()


@pytest.mark.asyncio
async def test_write_destination_repo_name_mismatch_rejected(
    make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_store
):
    """Attack A2: RepoContext has name repo-A, but create_issue_if_authorized called with repo-B."""
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context(owner="org-A", name="repo-A")
    store = make_dedup_store
    dr = store.check_and_reserve(f)

    client = AsyncMock()
    gate = GateResult(decision=GateDecision.ALLOW, reason="ALL_CONDITIONS_MET")

    with pytest.raises(Unauthorized):
        await create_issue_if_authorized(
            gate, dr, f, v, client, "org-A", "repo-B",
            evidence_result=er, repo_context=rc, dedup_store=store
        )
    client.create_issue.assert_not_called()


@pytest.mark.asyncio
async def test_write_destination_case_insensitive_match(
    make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_store
):
    """GitHub repository coordinates comparison must be case-insensitive."""
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context(owner="Test-Owner", name="Test-Repo")
    store = make_dedup_store
    dr = store.check_and_reserve(f)

    client = AsyncMock()
    client.get_repository.return_value = {"id": f.repository_id, "name": "test-repo"}
    client.search_issues.return_value = []
    client.get_issues.return_value = []
    client.create_issue.return_value = {"number": 1234, "body": "ok"}

    gate = GateResult(decision=GateDecision.ALLOW, reason="ALL_CONDITIONS_MET")
    res = await create_issue_if_authorized(
        gate, dr, f, v, client, "test-owner", "test-repo",
        evidence_result=er, repo_context=rc, dedup_store=store
    )
    assert res.issue_number == 1234
    assert client.create_issue.call_count == 1


@pytest.mark.asyncio
async def test_write_destination_live_repo_id_mismatch_rejected(
    make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_store
):
    """Attack B: get_repository returns a different numeric repository ID -> FAIL CLOSED."""
    f = make_finding(repository_id="12345")
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context(repository_id="12345", owner="test-owner", name="test-repo")
    store = make_dedup_store
    dr = store.check_and_reserve(f)

    client = AsyncMock()
    client.get_repository.return_value = {"id": "99999", "name": "test-repo"}
    gate = GateResult(decision=GateDecision.ALLOW, reason="ALL_CONDITIONS_MET")

    with pytest.raises(Unauthorized):
        await create_issue_if_authorized(
            gate, dr, f, v, client, "test-owner", "test-repo",
            evidence_result=er, repo_context=rc, dedup_store=store
        )
    client.create_issue.assert_not_called()


@pytest.mark.asyncio
async def test_write_destination_live_repo_id_timeout_fails_closed(
    make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_store
):
    """Attack C: get_repository times out -> FAIL CLOSED without POST."""
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    store = make_dedup_store
    dr = store.check_and_reserve(f)

    client = AsyncMock()
    client.get_repository.side_effect = GitHubRequestTimeoutError("get_repository timeout")
    gate = GateResult(decision=GateDecision.ALLOW, reason="ALL_CONDITIONS_MET")

    with pytest.raises((IssueCreationError, GitHubRequestTimeoutError)):
        await create_issue_if_authorized(
            gate, dr, f, v, client, "test-owner", "test-repo",
            evidence_result=er, repo_context=rc, dedup_store=store
        )
    client.create_issue.assert_not_called()


# ===========================================================================
# 14. DEDUP RESERVATION AUTHENTICITY ATTACKS (Sections 3 & 4)
# ===========================================================================

@pytest.mark.asyncio
async def test_dedup_reservation_forged_token_rejected(
    make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_store
):
    """Attack Section 4: Forged DedupResult token not issued by DedupStore must be rejected."""
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    store = make_dedup_store
    sig = compute_finding_signature(f)

    forged_dr = DedupResult(
        signature=sig,
        finding_id=f.finding_id,
        is_duplicate=False,
        reservation_token="forged_token_never_in_store",
        reason="ok",
    )

    client = AsyncMock()
    client.get_repository.return_value = {"id": f.repository_id, "name": "test-repo"}
    gate = GateResult(decision=GateDecision.ALLOW, reason="ALL_CONDITIONS_MET")

    with pytest.raises(Unauthorized):
        await create_issue_if_authorized(
            gate, forged_dr, f, v, client, "test-owner", "test-repo",
            evidence_result=er, repo_context=rc, dedup_store=store
        )
    client.create_issue.assert_not_called()


@pytest.mark.asyncio
async def test_dedup_reservation_stale_replaced_rejected(
    make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_store
):
    """A reservation token that was replaced by a newer reservation must fail authenticity validation."""
    f1 = make_finding(finding_id="F-001")
    store = make_dedup_store
    # Initial reservation
    dr1 = store.check_and_reserve(f1)
    original_token = dr1.reservation_token

    # Replaced by another reservation
    sig = compute_finding_signature(f1)
    store._reservations[sig]["reservation_token"] = "replaced_token_hex_value"

    v = make_verification(finding=f1)
    er = make_evidence_result(finding=f1)
    rc = make_repo_context()

    client = AsyncMock()
    client.get_repository.return_value = {"id": f1.repository_id, "name": "test-repo"}
    gate = GateResult(decision=GateDecision.ALLOW, reason="ALL_CONDITIONS_MET")

    # Attempt to use dr1 whose token has been replaced/cleared in store
    with pytest.raises(Unauthorized):
        await create_issue_if_authorized(
            gate, dr1, f1, v, client, "test-owner", "test-repo",
            evidence_result=er, repo_context=rc, dedup_store=store
        )
    client.create_issue.assert_not_called()


@pytest.mark.asyncio
async def test_dedup_reservation_genuine_happy_path(
    make_finding, make_verification, make_evidence_result, make_repo_context, make_dedup_store
):
    """A genuine reservation token issued by DedupStore succeeds at write boundary."""
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    store = make_dedup_store
    dr = store.check_and_reserve(f)

    client = AsyncMock()
    client.get_repository.return_value = {"id": f.repository_id, "name": "test-repo"}
    client.search_issues.return_value = []
    client.get_issues.return_value = []
    client.create_issue.return_value = {"number": 777, "body": "created"}

    gate = GateResult(decision=GateDecision.ALLOW, reason="ALL_CONDITIONS_MET")
    res = await create_issue_if_authorized(
        gate, dr, f, v, client, "test-owner", "test-repo",
        evidence_result=er, repo_context=rc, dedup_store=store
    )

    assert res.was_existing is False
    assert res.issue_number == 777
    assert client.create_issue.call_count == 1
