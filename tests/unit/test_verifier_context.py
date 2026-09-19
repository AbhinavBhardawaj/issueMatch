import pytest
from unittest.mock import AsyncMock
from app.domain.models import Finding, EvidenceItem, RepoContextFile, ContextCompleteness
from app.domain.states import FindingStatus, VerificationStatus
from app.github.client import (
    GitHubNotFoundError,
    GitHubRequestTimeoutError,
    GitHubRateLimitError,
    GitHubServerError,
)
from app.github.repo_fetcher import fetch_repo_context, FetchOutcome
from app.verifier.prompts import build_verifier_prompt
from app.verifier.agent import run_verifier


@pytest.fixture
def make_sample_finding():
    return Finding(
        finding_id="F-TEST-123",
        installation_id="10",
        repository_id="20",
        commit_sha="c" * 40,
        title="delete_user has no authorization",
        severity="high",
        category="security",
        file="app/users.py",
        function="delete_user",
        line=5,
        description="delete_user directly removes a user record without checking permissions.",
        expected_behavior="Must verify admin permissions before deleting user.",
        evidence=[
            EvidenceItem(
                file="app/users.py",
                line=5,
                snippet="def delete_user(user):\n    db.delete(user)",
            )
        ],
        confidence=0.85,
        status=FindingStatus.DISCOVERED,
    )


# ===========================================================================
# 1. COMMIT SHA PINNING FOR EVERY FETCH
# ===========================================================================

@pytest.mark.asyncio
async def test_all_targeted_fetches_pinned_to_finding_commit_sha(make_sample_finding):
    """Every fetch_file and tree inspection must pass ref=finding.commit_sha."""
    f = make_sample_finding
    client = AsyncMock()
    client.get_file_content.return_value = "def delete_user(user): db.delete(user)"
    client.get_repository_tree.return_value = ([], False)
    client.get_issues.return_value = []

    await fetch_repo_context(f, client, "org", "repo")

    # Check all get_file_content calls
    assert client.get_file_content.call_count >= 1
    for call in client.get_file_content.call_args_list:
        args, kwargs = call
        ref_arg = kwargs.get("ref") or (args[3] if len(args) > 3 else None)
        assert ref_arg == f.commit_sha, f"Fetch was not pinned to commit_sha: {ref_arg} != {f.commit_sha}"

    # Check get_repository_tree call
    if client.get_repository_tree.call_count > 0:
        args, kwargs = client.get_repository_tree.call_args
        tree_ref = kwargs.get("ref") or (args[2] if len(args) > 2 else None)
        assert tree_ref == f.commit_sha


# ===========================================================================
# 2. DISTINGUISH 404 FROM TRANSIENT FETCH FAILURE (Section 7)
# ===========================================================================

@pytest.mark.asyncio
async def test_distinguish_404_from_transient_failures(make_sample_finding):
    """
    404 -> ABSENT (cleanly omitted, not an error if auxiliary like README)
    timeout -> INCOMPLETE / PARTIAL context
    429 -> INCOMPLETE / PARTIAL context
    500 -> INCOMPLETE / PARTIAL context
    """
    f = make_sample_finding

    # Case A: README is 404 (absent). Primary file exists.
    # Context should remain COMPLETE because README is authoritatively absent.
    client_404 = AsyncMock()
    async def fake_get_file_404(owner, repo, path, ref):
        if path == "README.md":
            raise GitHubNotFoundError("README not found")
        return "content of " + path
    client_404.get_file_content.side_effect = fake_get_file_404
    client_404.get_repository_tree.return_value = ([], False)
    client_404.get_issues.return_value = []

    ctx_404 = await fetch_repo_context(f, client_404, "org", "repo")
    assert ctx_404.readme == ""
    assert ctx_404.context_completeness == ContextCompleteness.COMPLETE

    # Case B: Timeout on caller/dependency fetch -> INCOMPLETE
    client_timeout = AsyncMock()
    async def fake_get_file_timeout(owner, repo, path, ref):
        if path == "app/users.py":
            return "def delete_user(user): db.delete(user)"
        raise GitHubRequestTimeoutError("Connection timed out")
    client_timeout.get_file_content.side_effect = fake_get_file_timeout
    client_timeout.get_repository_tree.return_value = ([{"path": "app/caller.py", "type": "blob"}], False)
    client_timeout.get_issues.return_value = []

    ctx_timeout = await fetch_repo_context(f, client_timeout, "org", "repo")
    assert ctx_timeout.context_completeness == ContextCompleteness.PARTIAL
    assert any("TIMEOUT" in r for r in ctx_timeout.partial_reasons)

    # Case C: 429 rate limit on tree or file -> INCOMPLETE
    client_429 = AsyncMock()
    client_429.get_file_content.return_value = "def delete_user(user): db.delete(user)"
    client_429.get_repository_tree.side_effect = GitHubRateLimitError("Rate limit exceeded")
    client_429.get_issues.return_value = []

    ctx_429 = await fetch_repo_context(f, client_429, "org", "repo")
    assert ctx_429.context_completeness == ContextCompleteness.PARTIAL
    assert any("429" in r or "RATELIMIT" in r for r in ctx_429.partial_reasons)

    # Case D: 500 server error -> INCOMPLETE
    client_500 = AsyncMock()
    async def fake_get_file_500(owner, repo, path, ref):
        if path == "app/users.py":
            return "def delete_user(user): db.delete(user)"
        raise GitHubServerError("Internal server error")
    client_500.get_file_content.side_effect = fake_get_file_500
    client_500.get_repository_tree.return_value = ([{"path": "app/caller.py", "type": "blob"}], False)
    client_500.get_issues.return_value = []

    ctx_500 = await fetch_repo_context(f, client_500, "org", "repo")
    assert ctx_500.context_completeness == ContextCompleteness.PARTIAL
    assert any("500" in r or "SERVER_ERROR" in r for r in ctx_500.partial_reasons)


# ===========================================================================
# 3. REAL COUNTER-EVIDENCE TEST (Section 9)
# ===========================================================================

@pytest.mark.asyncio
async def test_real_caller_counter_evidence_discovered_and_not_verified(make_sample_finding):
    """
    Function delete_user looks vulnerable in isolation.
    Targeted Verifier context builder discovers caller:
        if not current_user.is_admin:
            raise Forbidden()
        delete_user(target)
    The prompt receives the caller counter-evidence.
    The adversarial verifier rejects the unsafe claim.
    """
    f = make_sample_finding

    repo_files = {
        "app/users.py": (
            "def delete_user(user):\n"
            "    db.delete(user)\n"
        ),
        "app/api/endpoints.py": (
            "from app.users import delete_user\n\n"
            "def handle_delete_endpoint(target, current_user):\n"
            "    if not current_user.is_admin:\n"
            "        raise Forbidden('Admin required')\n"
            "    delete_user(target)\n"
        ),
        "tests/test_users.py": (
            "def test_non_admin_cannot_delete():\n"
            "    pass\n"
        ),
        "README.md": "# User Service\nAll deletions require admin authorization enforced at endpoint layer.\n",
    }

    client = AsyncMock()
    async def fake_get_content(owner, repo, path, ref):
        if path in repo_files:
            return repo_files[path]
        raise GitHubNotFoundError(f"{path} not found")

    client.get_file_content.side_effect = fake_get_content
    client.get_repository_tree.return_value = (
        [{"path": p, "type": "blob"} for p in repo_files.keys()],
        False
    )
    client.get_issues.return_value = []

    # 1. Fetch targeted verifier context
    repo_context = await fetch_repo_context(f, client, "org", "repo")

    # Verify that the caller file was discovered and included in repo_context
    all_paths = [cf.path for cf in repo_context.files + repo_context.caller_files]
    assert "app/api/endpoints.py" in all_paths

    # 2. Build verifier prompt and verify caller code & line numbers are present
    prompt = build_verifier_prompt(f, repo_context)
    assert "app/api/endpoints.py" in prompt
    assert "if not current_user.is_admin" in prompt
    assert "<untrusted_repository_context>" in prompt
    assert "</untrusted_repository_context>" in prompt

    # 3. Pass to Verifier with an adversarial LLM that inspects caller protection
    class AdversarialCounterEvidenceLLM:
        async def complete(self, system: str, user: str) -> str:
            if "if not current_user.is_admin" in user:
                return '{"status": "REJECTED", "reason": "Authorization is checked in caller handle_delete_endpoint before calling delete_user.", "confidence": 0.95}'
            return '{"status": "VERIFIED", "reason": "No authorization check found", "confidence": 0.9}'

    verification = await run_verifier(f, repo_context, AdversarialCounterEvidenceLLM())
    assert verification.status == VerificationStatus.REJECTED
    assert "Authorization is checked in caller" in verification.reason
