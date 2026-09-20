import pytest
from unittest.mock import AsyncMock
from tests.integration.test_live_scout_verifier_e2e import (
    GuardedGitHubClient,
    LiveWriteDisabledError,
)


@pytest.mark.asyncio
async def test_read_only_client_does_not_call_real_create_issue():
    """Test H: Read-only client intercepts create_issue without calling real GitHub API, recording simulated write and returning sentinel."""
    mock_inner = AsyncMock()
    mock_inner.create_issue.return_value = {"number": 123, "html_url": "https://github.com/test/repo/issues/123"}

    guarded_client = GuardedGitHubClient(mock_inner, allow_writes=False)

    result = await guarded_client.create_issue("owner", "repo", "Bug Title", "Bug Body")

    # Assert real create_issue was never called
    mock_inner.create_issue.assert_not_called()
    # Assert simulated write recorded
    assert guarded_client.simulated_issue_writes == 1
    assert len(guarded_client.recorded_calls) == 1
    assert guarded_client.recorded_calls[0]["title"] == "Bug Title"
    # Assert synthetic sentinel issue returned with negative number
    assert result["number"] < 0
    assert result["number"] == -1
    assert "dry-run" in result["html_url"]


@pytest.mark.asyncio
async def test_write_enabled_wrapper_delegates_correctly():
    """Test J: Write-enabled wrapper delegates correctly using mocks only (no real issue created)."""
    mock_inner = AsyncMock()
    mock_inner.create_issue.return_value = {"number": 456, "html_url": "https://github.com/test/repo/issues/456"}

    guarded_client = GuardedGitHubClient(mock_inner, allow_writes=True)

    result = await guarded_client.create_issue("owner", "repo", "Bug Title", "Bug Body")

    assert result["number"] == 456
    mock_inner.create_issue.assert_called_once_with("owner", "repo", "Bug Title", "Bug Body", labels=None)
    assert guarded_client.simulated_issue_writes == 0


@pytest.mark.asyncio
async def test_read_only_authorization_not_reported_as_verifier_execution_failed():
    """Test I: In read-only mode, authorized finding issue creation is simulated without raising or reporting VERIFIER_EXECUTION_FAILED."""
    from app.services.issue_creator import create_issue_if_authorized
    from app.services.issue_gate import GateResult, GateDecision, DedupResult
    from app.services.write_journal import InMemoryIssueWriteJournal
    from app.services.issue_gate import InMemoryDedupStore
    from app.domain.models import Finding, Verification, RepoContext, RepoContextFile, EvidenceItem, VerifierEvidence
    from app.domain.states import VerificationStatus
    from app.verifier.evidence import EvidenceValidationResult, EvidenceItemResult, EvidenceStatus

    mock_raw_client = AsyncMock()
    mock_raw_client.get_repository.return_value = {"id": 12345, "default_branch": "main"}
    guarded_client = GuardedGitHubClient(mock_raw_client, allow_writes=False)

    journal = InMemoryIssueWriteJournal()
    dedup_store = InMemoryDedupStore()

    finding = Finding(
        finding_id="find-123",
        installation_id="inst-1",
        repository_id="12345",
        commit_sha="a" * 40,
        file="src/app.py",
        line=2,
        title="Null pointer crash",
        description="Potential crash",
        expected_behavior="Handle None safely",
        severity="critical",
        confidence=0.95,
        evidence=[EvidenceItem(file="src/app.py", line=2, snippet="x.foo()")],
    )
    verification = Verification(
        verification_id="ver-123",
        finding_id=finding.finding_id,
        installation_id=finding.installation_id,
        repository_id=finding.repository_id,
        commit_sha=finding.commit_sha,
        status=VerificationStatus.VERIFIED,
        reason="Definite bug",
        confidence=0.95,
        supporting_evidence=[VerifierEvidence(file="src/app.py", line=2, snippet="x.foo()")],
    )
    context = RepoContext(
        repository_id="12345",
        installation_id="inst-1",
        owner="test-owner",
        name="test-repo",
        commit_sha="a" * 40,
        default_branch="main",
        files=[RepoContextFile(path="src/app.py", content="def run():\n    x.foo()\n", size_bytes=24)],
    )

    gate_result = GateResult(decision=GateDecision.ALLOW, reason="High impact authorized bug")
    dedup_result = await dedup_store.check_and_reserve(finding)
    evidence_result = EvidenceValidationResult(
        finding_id=finding.finding_id,
        commit_sha=finding.commit_sha,
        results=[
            EvidenceItemResult(
                file="src/app.py",
                line=2,
                snippet="x.foo()",
                file_exists=True,
                line_exists=True,
                snippet_found=True,
                status=EvidenceStatus.SUPPORTED,
            )
        ],
        overall=EvidenceStatus.SUPPORTED,
    )

    # Execute issue creation through guarded client
    result = await create_issue_if_authorized(
        gate_result=gate_result,
        dedup_result=dedup_result,
        finding=finding,
        verification=verification,
        github_client=guarded_client,
        owner="test-owner",
        repo_name="test-repo",
        evidence_result=evidence_result,
        repo_context=context,
        dedup_store=dedup_store,
        write_journal=journal,
    )

    # Verify no real issue was created, and execution did NOT fail
    mock_raw_client.create_issue.assert_not_called()
    assert guarded_client.simulated_issue_writes == 1
    assert result.issue_number == -1
    assert result.was_existing is False


@pytest.mark.asyncio
async def test_contradictory_write_flags_fails_immediately(monkeypatch):
    """Step 5: LIVE_E2E_EXPECT_ISSUE_CREATION=1 with LIVE_E2E_ALLOW_ISSUE_WRITES=0 must fail immediately before any client/LLM setup."""
    from tests.integration.test_live_scout_verifier_e2e import test_live_scout_and_verifier_pipeline

    monkeypatch.setenv("LIVE_E2E_EXPECT_ISSUE_CREATION", "1")
    monkeypatch.setenv("LIVE_E2E_ALLOW_ISSUE_WRITES", "0")

    with pytest.raises(pytest.fail.Exception) as exc_info:
        await test_live_scout_and_verifier_pipeline()

    assert "LIVE_E2E_EXPECT_ISSUE_CREATION=1 requires LIVE_E2E_ALLOW_ISSUE_WRITES=1" in str(exc_info.value)


@pytest.mark.asyncio
async def test_identical_before_and_commit_sha_fails_before_llm(monkeypatch):
    """Step 7: LIVE_E2E_BEFORE_SHA == LIVE_E2E_COMMIT_SHA must fail before any LLM call."""
    from tests.integration.test_live_scout_verifier_e2e import test_live_scout_and_verifier_pipeline

    same_sha = "a" * 40
    monkeypatch.setenv("LIVE_E2E_ALLOW_ISSUE_WRITES", "0")
    monkeypatch.setenv("LIVE_E2E_EXPECT_ISSUE_CREATION", "0")
    monkeypatch.setenv("LIVE_E2E_OWNER", "test-owner")
    monkeypatch.setenv("LIVE_E2E_REPO", "sandbox-repo")
    monkeypatch.setenv("LIVE_E2E_INSTALLATION_ID", "123")
    monkeypatch.setenv("LIVE_E2E_BEFORE_SHA", same_sha)
    monkeypatch.setenv("LIVE_E2E_COMMIT_SHA", same_sha)
    monkeypatch.setenv("GITHUB_APP_ID", "100")

    with pytest.raises(pytest.fail.Exception) as exc_info:
        await test_live_scout_and_verifier_pipeline()

    assert "LIVE_E2E_BEFORE_SHA and LIVE_E2E_COMMIT_SHA must refer to different commits" in str(exc_info.value)


@pytest.mark.asyncio
async def test_expected_changed_file_prevalidation_fails_before_llm(monkeypatch):
    """Step 8: Missing expected changed file in compare_commits result must fail before process_push."""
    from unittest.mock import patch, MagicMock
    from tests.integration.test_live_scout_verifier_e2e import test_live_scout_and_verifier_pipeline

    monkeypatch.setenv("LIVE_E2E_ALLOW_ISSUE_WRITES", "0")
    monkeypatch.setenv("LIVE_E2E_EXPECT_ISSUE_CREATION", "0")
    monkeypatch.setenv("LIVE_E2E_OWNER", "test-owner")
    monkeypatch.setenv("LIVE_E2E_REPO", "sandbox-repo")
    monkeypatch.setenv("LIVE_E2E_INSTALLATION_ID", "123")
    monkeypatch.setenv("LIVE_E2E_EXPECTED_REPO_ID", "12345")
    monkeypatch.setenv("LIVE_E2E_BEFORE_SHA", "a" * 40)
    monkeypatch.setenv("LIVE_E2E_COMMIT_SHA", "b" * 40)
    monkeypatch.setenv("LIVE_E2E_EXPECTED_CHANGED_FILE", "missing_file.py")
    monkeypatch.setenv("GITHUB_APP_ID", "100")
    monkeypatch.setenv("GITHUB_PRIVATE_KEY", "-----BEGIN RSA PRIVATE KEY-----\nkey\n-----END RSA PRIVATE KEY-----")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")

    mock_client = AsyncMock()
    mock_client.get_repository.return_value = {"id": 12345, "default_branch": "main"}
    # compare_commits returns files list that does NOT contain missing_file.py
    mock_client.compare_commits.return_value = {
        "files": [{"filename": "other_file.py", "status": "modified"}]
    }

    mock_authenticator = MagicMock()
    mock_authenticator.create_installation_client = AsyncMock(return_value=mock_client)

    with patch("tests.integration.test_live_scout_verifier_e2e.GitHubAppAuthenticator", return_value=mock_authenticator), \
         patch("tests.integration.test_live_scout_verifier_e2e.build_scout_service") as mock_build_scout:
        mock_scout_svc = AsyncMock()
        mock_build_scout.return_value = mock_scout_svc

        with pytest.raises(pytest.fail.Exception) as exc_info:
            await test_live_scout_and_verifier_pipeline()

        assert "Expected changed file 'missing_file.py' not found in comparison files" in str(exc_info.value)
        # Verify scout_service.process_push was NEVER called
        mock_scout_svc.process_push.assert_not_called()

