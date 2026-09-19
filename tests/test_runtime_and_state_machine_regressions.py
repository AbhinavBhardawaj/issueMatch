import asyncio
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from app.main import app, create_application
from app.domain.models import Finding, EvidenceItem, RepoContext, RepoContextFile, ExistingIssue
from app.domain.states import FindingStatus, VerificationStatus
from app.domain.transitions import IllegalTransitionError, transition_finding
from app.services.pipeline import VerifierPipeline
from app.services.issue_gate import InMemoryDedupStore
from app.scout.agent import ScoutAgent
from app.scout.service import ScoutService
from app.scout.models import ChangedFile
from app.scout.schemas import ScoutResponse, ScoutFindingDraft, ScoutEvidenceDraft
from app.github.events import GitHubPushEvent
from app.verifier.deterministic import post_verifier_recheck, RecheckStatus, RecheckResult
from app.verifier.evidence import validate_evidence


# ---------------------------------------------------------------------------
# TEST A: Default ASGI application exposes POST /webhooks/github
# ---------------------------------------------------------------------------
def test_a_default_asgi_app_exposes_webhook():
    """Default ASGI application from app.main:app must expose POST /webhooks/github."""
    routes = [route.path for route in app.routes]
    assert "/webhooks/github" in routes, f"/webhooks/github not in {routes}"

    # Dev endpoints must NOT be exposed by default
    assert "/api/scout" not in routes
    assert "/api/verify" not in routes

    # Calling /webhooks/github without signature should return 401 Unauthorized (not 404 Not Found)
    client = TestClient(app)
    resp = client.post("/webhooks/github", content=b"{}", headers={"content-type": "application/json"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# TEST B: Valid signed push can reach Scout orchestration
# ---------------------------------------------------------------------------
def test_b_valid_signed_push_reaches_scout_orchestration():
    """A valid signed push webhook must reach the ScoutService orchestration path."""
    mock_scout_service = MagicMock()
    mock_scout_service.process_push = AsyncMock()

    secret = "test-webhook-secret-123"
    test_app = create_application(webhook_secret=secret, scout_service=mock_scout_service)
    client = TestClient(test_app)

    payload = {
        "ref": "refs/heads/main",
        "before": "1" * 40,
        "after": "2" * 40,
        "forced": False,
        "deleted": False,
        "repository": {
            "id": 12345,
            "name": "my-repo",
            "owner": {"login": "my-org"},
            "default_branch": "main",
        },
        "installation": {"id": 67890},
    }
    raw_body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()

    headers = {
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": "del-test-b-1",
        "X-Hub-Signature-256": sig,
        "content-type": "application/json",
    }

    resp = client.post("/webhooks/github", content=raw_body, headers=headers)
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted"}
    mock_scout_service.process_push.assert_called_once()
    event_arg = mock_scout_service.process_push.call_args[0][0]
    assert event_arg.repository_id == 12345
    assert event_arg.installation_id == 67890


# ---------------------------------------------------------------------------
# TEST C: Contradicted deterministic evidence does NOT raise IllegalTransitionError
# Expected: DISCOVERED -> VERIFYING -> REJECTED
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_c_contradicted_evidence_no_illegal_transition(make_finding):
    """
    When deterministic evidence validation contradicts finding evidence,
    transition must be DISCOVERED -> VERIFYING -> REJECTED without raising IllegalTransitionError.
    """
    finding = make_finding(
        status=FindingStatus.DISCOVERED,
        evidence=[
            EvidenceItem(
                file="src/auth.py",
                line=99,
                snippet="nonexistent_code_snippet()",
            )
        ],
    )
    repo_context = RepoContext(
        repository_id=finding.repository_id,
        installation_id=finding.installation_id,
        owner="test-owner",
        name="test-repo",
        commit_sha=finding.commit_sha,
        files=[
            RepoContextFile(path="src/auth.py", content="x = 1\n", size_bytes=6)
        ],
    )

    recorded_transitions = []
    original_transition = transition_finding

    def spy_transition(f, new_status):
        recorded_transitions.append((f.status, new_status))
        return original_transition(f, new_status)

    mock_llm = AsyncMock()
    mock_write_client = AsyncMock()
    pipeline = VerifierPipeline(llm_provider=mock_llm, dedup_store=InMemoryDedupStore())

    with patch("app.services.pipeline.transition_finding", side_effect=spy_transition):
        # Must not raise IllegalTransitionError
        result = await pipeline.run(
            finding,
            repo_context,
            "test-owner",
            "test-repo",
            github_write_client=mock_write_client,
        )

    assert result is None
    assert (FindingStatus.DISCOVERED, FindingStatus.VERIFYING) in recorded_transitions
    assert (FindingStatus.VERIFYING, FindingStatus.REJECTED) in recorded_transitions
    assert (FindingStatus.DISCOVERED, FindingStatus.REJECTED) not in recorded_transitions


# ---------------------------------------------------------------------------
# TEST D: Verifier returns VERIFIED but post-recheck fails
# Expected: VERIFYING -> REJECTED (not VERIFIED -> REJECTED)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_d_verifier_verified_post_recheck_fails_verifying_to_rejected(
    make_finding, make_repo_context
):
    """
    When Verifier returns VERIFIED but post-recheck fails (e.g. binding mismatch),
    the transition must be VERIFYING -> REJECTED, NOT VERIFIED -> REJECTED.
    """
    f = make_finding(status=FindingStatus.DISCOVERED)
    rc = make_repo_context()

    # Verifier LLM returns VERIFIED
    verifier_json = json.dumps({
        "status": "VERIFIED",
        "reason": "Vulnerability confirmed by verifier",
        "supporting_evidence": [{"file": "src/auth.py", "line": 82, "snippet": "decoded = jwt.decode(...)"}],
        "counter_evidence": [],
        "duplicate_issue": False,
        "confidence": 0.95,
    })
    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=verifier_json)

    recorded_transitions = []
    original_transition = transition_finding

    def spy_transition(finding, new_status):
        recorded_transitions.append((finding.status, new_status))
        return original_transition(finding, new_status)

    mock_write_client = AsyncMock()
    pipeline = VerifierPipeline(llm_provider=mock_llm, dedup_store=InMemoryDedupStore())

    # Simulate post_verifier_recheck failure (e.g. binding mismatch or recheck fails)
    with patch("app.services.pipeline.post_verifier_recheck", return_value=RecheckResult(status=RecheckStatus.FAIL, reason="REPO_CONTEXT_INSTALLATION_MISMATCH")):
        with patch("app.services.pipeline.transition_finding", side_effect=spy_transition):
            result = await pipeline.run(
                f,
                rc,
                "test-owner",
                "test-repo",
                github_write_client=mock_write_client,
            )

    assert result is None
    # Must have transitioned VERIFYING -> REJECTED
    assert (FindingStatus.VERIFYING, FindingStatus.REJECTED) in recorded_transitions
    # Must NOT have transitioned VERIFIED -> REJECTED
    assert (FindingStatus.VERIFIED, FindingStatus.REJECTED) not in recorded_transitions


# ---------------------------------------------------------------------------
# TEST E: Verified valid finding uses the correct installation-specific GitHub write client
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_e_verified_finding_uses_installation_specific_write_client(
    make_finding, make_repo_context
):
    """Verified finding creates an issue using the explicitly passed installation write client."""
    f = make_finding(status=FindingStatus.DISCOVERED)
    rc = make_repo_context()

    verifier_json = json.dumps({
        "status": "VERIFIED",
        "reason": "Bug verified",
        "supporting_evidence": [{"file": "src/auth.py", "line": 82, "snippet": "decoded = jwt.decode(...)"}],
        "counter_evidence": [],
        "duplicate_issue": False,
        "confidence": 0.95,
    })
    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=verifier_json)

    installation_client = AsyncMock()
    installation_client.get_repository.return_value = {"id": "R-123", "owner": "test-owner", "name": "test-repo"}
    installation_client.search_issues.return_value = []
    installation_client.create_issue.return_value = {"number": 777, "html_url": "https://github.com/test-owner/test-repo/issues/777"}

    pipeline = VerifierPipeline(llm_provider=mock_llm, dedup_store=InMemoryDedupStore())
    result = await pipeline.run(
        f,
        rc,
        "test-owner",
        "test-repo",
        github_write_client=installation_client,
    )

    assert result is not None
    assert result.issue_number == 777
    installation_client.create_issue.assert_called_once()
    call_args = installation_client.create_issue.call_args
    assert call_args[0][0] == "test-owner"
    assert call_args[0][1] == "test-repo"
    assert call_args[0][2] == f.title


# ---------------------------------------------------------------------------
# TEST F: Two concurrent pushes with different installations use different clients
# Assert no client leakage
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_f_concurrent_pushes_use_different_installation_clients_no_leakage():
    """
    Two concurrent pushes from installation 111 and 222 must use distinct
    write clients, with no capability leakage across requests.
    """
    client_111 = AsyncMock()
    client_111.get_repository.return_value = {"id": 101, "name": "repo1", "owner": {"login": "org1"}}
    client_111.search_issues.return_value = []
    client_111.get_issues.return_value = []
    client_111.get_file_content.return_value = "def foo():\n    return 42\n"
    client_111.create_issue.return_value = {"number": 1111, "html_url": "https://github.com/org1/repo1/issues/1111"}

    client_222 = AsyncMock()
    client_222.get_repository.return_value = {"id": 202, "name": "repo2", "owner": {"login": "org2"}}
    client_222.search_issues.return_value = []
    client_222.get_issues.return_value = []
    client_222.get_file_content.return_value = "def foo():\n    return 42\n"
    client_222.create_issue.return_value = {"number": 2222, "html_url": "https://github.com/org2/repo2/issues/2222"}

    async def write_client_factory(installation_id: int):
        if installation_id == 111:
            return client_111
        elif installation_id == 222:
            return client_222
        raise ValueError(f"Unknown installation {installation_id}")

    # Mock read client
    mock_read_client = AsyncMock()
    mock_read_client.get_repository.side_effect = lambda owner, repo: {"id": 101 if repo == "repo1" else 202, "name": repo, "owner": {"login": owner}}
    mock_read_client.compare_commits.return_value = {"files": [{"filename": "main.py", "status": "modified"}]}
    mock_read_client.get_repository_tree.return_value = ([{"path": "main.py", "type": "blob"}], False)
    mock_read_client.get_file_content.return_value = "def foo():\n    return 42\n"

    # Mock ScoutAgent returning a draft
    scout_draft = ScoutFindingDraft(
        category="behavioral_bug",
        title="Sample bug",
        severity="high",
        file="main.py",
        function="foo",
        line=2,
        description="Sample description",
        expected_behavior="Expected",
        evidence=[ScoutEvidenceDraft(file="main.py", line=2, snippet="return 42")],
        confidence=0.9,
        impact="major",
        impact_reason="Crash",
        observable_behavior="Error",
        affected_user_or_system="Users",
        actionable=True,
        actionability_reason="Fix",
        regression_likelihood="introduced_by_push",
        expected_behavior_basis="language_semantics",
        expected_behavior_evidence="Python rules",
        claim_scope="local",
        depends_on_absence=False,
    )

    mock_scout_agent = MagicMock()
    mock_scout_agent.discover = AsyncMock(return_value=ScoutResponse(findings=[scout_draft]))

    # Verifier LLM
    verifier_json = json.dumps({
        "status": "VERIFIED",
        "reason": "Confirmed",
        "supporting_evidence": [{"file": "main.py", "line": 2, "snippet": "return 42"}],
        "counter_evidence": [],
        "duplicate_issue": False,
        "confidence": 0.95,
    })
    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=verifier_json)

    shared_dedup = InMemoryDedupStore()
    pipeline = VerifierPipeline(llm_provider=mock_llm, dedup_store=shared_dedup)

    service = ScoutService(
        scout_agent=mock_scout_agent,
        verifier_pipeline=pipeline,
        read_client_factory=AsyncMock(return_value=mock_read_client),
        downstream_write_client_factory=write_client_factory,
    )

    event_111 = GitHubPushEvent(
        delivery_id="del-111",
        installation_id=111,
        repository_id=101,
        repository_owner="org1",
        repository_name="repo1",
        default_branch="main",
        before_sha="a" * 40,
        after_sha="b" * 40,
        ref="refs/heads/main",
        forced=False,
        deleted=False,
    )

    event_222 = GitHubPushEvent(
        delivery_id="del-222",
        installation_id=222,
        repository_id=202,
        repository_owner="org2",
        repository_name="repo2",
        default_branch="main",
        before_sha="c" * 40,
        after_sha="d" * 40,
        ref="refs/heads/main",
        forced=False,
        deleted=False,
    )

    # Run concurrently
    res1, res2 = await asyncio.gather(
        service.process_push(event_111),
        service.process_push(event_222),
    )

    # Client 111 was called with org1/repo1 only
    assert client_111.create_issue.call_count == 1
    call1 = client_111.create_issue.call_args[0]
    assert call1[0] == "org1"
    assert call1[1] == "repo1"

    # Client 222 was called with org2/repo2 only
    assert client_222.create_issue.call_count == 1
    call2 = client_222.create_issue.call_args[0]
    assert call2[0] == "org2"
    assert call2[1] == "repo2"


# ---------------------------------------------------------------------------
# TEST G: ScoutAgent has NO GitHub write client capability
# ---------------------------------------------------------------------------
def test_g_scout_agent_has_no_github_write_client():
    """ScoutAgent must have zero GitHub write client references, methods, or parameters."""
    agent = ScoutAgent(llm_provider=MagicMock())
    assert not hasattr(agent, "github_write_client")
    assert not hasattr(agent, "github_client")
    assert not hasattr(agent, "create_issue")

    # Inspect class attributes and init signature
    import inspect
    init_sig = inspect.signature(ScoutAgent.__init__)
    for param_name in init_sig.parameters:
        assert "write" not in param_name.lower()
        assert "client" not in param_name.lower() or "llm" in param_name.lower()
