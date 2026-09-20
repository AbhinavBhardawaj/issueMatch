import pytest
from unittest.mock import AsyncMock
from tests.integration.test_live_scout_verifier_e2e import (
    GuardedGitHubClient,
    LiveWriteDisabledError,
)


@pytest.mark.asyncio
async def test_live_write_guard_blocks_issue_creation_when_disabled():
    """Assert that GuardedGitHubClient raises LiveWriteDisabledError when allow_writes is False."""
    mock_inner = AsyncMock()
    mock_inner.create_issue.return_value = {"number": 123, "html_url": "https://github.com/test/repo/issues/123"}

    guarded_client = GuardedGitHubClient(mock_inner, allow_writes=False)

    with pytest.raises(LiveWriteDisabledError) as exc_info:
        await guarded_client.create_issue("owner", "repo", "Bug Title", "Bug Body")

    assert "LIVE_E2E_ALLOW_ISSUE_WRITES != '1'" in str(exc_info.value)
    mock_inner.create_issue.assert_not_called()


@pytest.mark.asyncio
async def test_live_write_guard_allows_issue_creation_when_enabled():
    """Assert that GuardedGitHubClient passes through create_issue when allow_writes is True."""
    mock_inner = AsyncMock()
    mock_inner.create_issue.return_value = {"number": 456, "html_url": "https://github.com/test/repo/issues/456"}

    guarded_client = GuardedGitHubClient(mock_inner, allow_writes=True)

    result = await guarded_client.create_issue("owner", "repo", "Bug Title", "Bug Body")

    assert result["number"] == 456
    mock_inner.create_issue.assert_called_once_with("owner", "repo", "Bug Title", "Bug Body")


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

