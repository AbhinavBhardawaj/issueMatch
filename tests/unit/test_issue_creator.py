import pytest
import asyncio
from app.services.issue_creator import (
    create_issue_if_authorized,
    IssueCreationResult,
    Unauthorized,
    IssueCreationError,
    GitHubAPIError,
    _format_issue_body,
    _sanitize_mentions
)
from app.services.issue_gate import GateResult, GateDecision
from app.domain.models import Finding
from typing import Optional

class FakeGitHubClient:
    def __init__(self):
        self.issues_created = []
        self.search_results = []
        self.create_error: Optional[Exception | list[Exception | None]] = None
        self.create_call_count = 0

    async def get_repository(self, owner: str, repo: str) -> dict:
        return {"id": "R-123", "owner": owner, "name": repo}

    async def create_issue(self, owner: str, repo: str, title: str, body: str, labels: list[str] | None = None) -> dict:
        self.create_call_count += 1
        if self.create_error:
            if isinstance(self.create_error, list):
                err = self.create_error.pop(0)
                if err:
                    raise err
            else:
                raise self.create_error
        
        issue = {"number": 42 + len(self.issues_created), "body": body}
        self.issues_created.append(issue)
        return issue

    async def search_issues(self, owner: str, repo: str, query: str) -> list[dict]:
        return self.search_results

@pytest.fixture
def fake_client():
    return FakeGitHubClient()

def test_ic1_gate_deny(make_finding, make_verification, make_dedup_result, make_gate_result, make_evidence_result, make_repo_context, make_dedup_store, fake_client):
    finding = make_finding()
    verification = make_verification(finding)
    gate_result = make_gate_result(decision=GateDecision.DENY, reason="EVIDENCE_NOT_SUPPORTED")
    dedup_result = make_dedup_result(finding)
    er = make_evidence_result(finding)
    rc = make_repo_context()

    async def run():
        with pytest.raises(Unauthorized, match="Gate denied"):
            await create_issue_if_authorized(
                gate_result, dedup_result, finding, verification, fake_client, "test-owner", "test-repo",
                evidence_result=er, repo_context=rc, dedup_store=make_dedup_store
            )
    asyncio.run(run())
    assert len(fake_client.issues_created) == 0

def test_ic2_marker_found(make_finding, make_verification, make_dedup_result, make_gate_result, make_evidence_result, make_repo_context, make_dedup_store, fake_client):
    finding = make_finding()
    verification = make_verification(finding)
    gate_result = make_gate_result()
    dedup_result = make_dedup_result(finding)
    er = make_evidence_result(finding)
    rc = make_repo_context()
    
    marker = f"<!-- opencontrib:finding:{finding.finding_id}:v:{verification.verification_id} -->"
    fake_client.search_results.append({"number": 99, "body": marker})

    result = asyncio.run(create_issue_if_authorized(
        gate_result, dedup_result, finding, verification, fake_client, "test-owner", "test-repo",
        evidence_result=er, repo_context=rc, dedup_store=make_dedup_store
    ))
    
    assert result.issue_number == 99
    assert result.was_existing is True
    assert len(fake_client.issues_created) == 0

def test_ic3_normal_creation(make_finding, make_verification, make_dedup_result, make_gate_result, make_evidence_result, make_repo_context, make_dedup_store, fake_client):
    finding = make_finding()
    verification = make_verification(finding)
    gate_result = make_gate_result()
    dedup_result = make_dedup_result(finding)
    er = make_evidence_result(finding)
    rc = make_repo_context()

    result = asyncio.run(create_issue_if_authorized(
        gate_result, dedup_result, finding, verification, fake_client, "test-owner", "test-repo",
        evidence_result=er, repo_context=rc, dedup_store=make_dedup_store
    ))
    
    assert result.issue_number == 42
    assert result.was_existing is False
    assert len(fake_client.issues_created) == 1

def test_ic4_500_retry_success(make_finding, make_verification, make_dedup_result, make_gate_result, make_evidence_result, make_repo_context, make_dedup_store, fake_client):
    from app.services.write_journal import UnresolvedWriteIntentError

    finding = make_finding()
    verification = make_verification(finding)
    gate_result = make_gate_result()
    dedup_result = make_dedup_result(finding)
    er = make_evidence_result(finding)
    rc = make_repo_context()
    
    fake_client.create_error = [GitHubAPIError("Server Error", 500), None]

    with pytest.raises(UnresolvedWriteIntentError):
        asyncio.run(create_issue_if_authorized(
            gate_result, dedup_result, finding, verification, fake_client, "test-owner", "test-repo",
            evidence_result=er, repo_context=rc, dedup_store=make_dedup_store
        ))
    
    # 5xx must not blindly issue a second POST (create_call_count == 1)
    assert fake_client.create_call_count == 1
    assert len(fake_client.issues_created) == 0

def test_ic5_429_retry_after(make_finding, make_verification, make_dedup_result, make_gate_result, make_evidence_result, make_repo_context, make_dedup_store, fake_client):
    finding = make_finding()
    verification = make_verification(finding)
    gate_result = make_gate_result()
    dedup_result = make_dedup_result(finding)
    er = make_evidence_result(finding)
    rc = make_repo_context()
    
    fake_client.create_error = [GitHubAPIError("Rate Limit", 429, retry_after=0), None]

    result = asyncio.run(create_issue_if_authorized(
        gate_result, dedup_result, finding, verification, fake_client, "test-owner", "test-repo",
        evidence_result=er, repo_context=rc, dedup_store=make_dedup_store
    ))
    
    assert result.issue_number == 42
    assert fake_client.create_call_count == 2
    assert len(fake_client.issues_created) == 1

def test_ic6_timeout_reconciliation(make_finding, make_verification, make_dedup_result, make_gate_result, make_evidence_result, make_repo_context, make_dedup_store):
    from app.github.client import GitHubAmbiguousWriteError

    finding = make_finding()
    verification = make_verification(finding)
    gate_result = make_gate_result()
    dedup_result = make_dedup_result(finding)
    er = make_evidence_result(finding)
    rc = make_repo_context()
    
    sig_marker = f"<!-- opencontrib:signature:{dedup_result.signature} -->"
    
    class DynamicFakeClient(FakeGitHubClient):
        async def create_issue(self, owner, repo, title, body, labels=None):
            self.create_call_count += 1
            self.search_results.append({"number": 100, "body": sig_marker})
            raise GitHubAmbiguousWriteError("Timeout during issue creation POST")
            
    client = DynamicFakeClient()
    
    result = asyncio.run(create_issue_if_authorized(
        gate_result, dedup_result, finding, verification, client, "test-owner", "test-repo",
        evidence_result=er, repo_context=rc, dedup_store=make_dedup_store
    ))
    
    assert result.issue_number == 100
    assert result.was_existing is True
    assert client.create_call_count == 1
    assert len(client.issues_created) == 0

def test_ic7_sanitize_mentions():
    text = "Hello @everyone and @here, also `@code`."
    sanitized = _sanitize_mentions(text)
    assert sanitized == "Hello `@everyone` and `@here`, also `@code`."

def test_ic8_body_has_marker(make_finding, make_verification):
    finding = make_finding()
    verification = make_verification(finding)
    
    body = _format_issue_body(finding, verification)
    marker = f"<!-- opencontrib:finding:{finding.finding_id}:v:{verification.verification_id} -->"
    assert marker in body

def test_ic9_body_format(make_finding, make_verification):
    finding = make_finding()
    verification = make_verification(finding)
    body = _format_issue_body(finding, verification)
    
    assert "## Problem" in body
    assert finding.description in body
    assert "## Expected Behavior" in body
    assert finding.expected_behavior in body
    assert "## Evidence" in body
    assert "## Why This May Be a Bug" in body
    assert "## Independent Verification" in body
    assert verification.reason in body
    assert "## Suggested Investigation" in body

def test_ic10_no_llm_import():
    import sys
    from pathlib import Path
    
    creator_path = Path("app/services/issue_creator.py")
    if creator_path.exists():
        content = creator_path.read_text()
        assert "LLMProvider" not in content
        assert "StubLLMProvider" not in content
        assert "agent" not in content
