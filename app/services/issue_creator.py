import re
import asyncio
from typing import Protocol
from pydantic import BaseModel, ConfigDict
from app.domain.models import Finding, Verification
from app.services.issue_gate import GateResult, GateDecision, DedupResult

class IssueWriteClient(Protocol):
    async def create_issue(self, owner: str, repo: str, title: str, body: str, labels: list[str] | None = None) -> dict: ...
    async def search_issues(self, owner: str, repo: str, query: str) -> list[dict]: ...

class IssueCreationResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    
    issue_number: int
    was_existing: bool
    finding_id: str

class IssueCreationError(Exception):
    pass

class Unauthorized(IssueCreationError):
    pass

class GitHubAPIError(Exception):
    def __init__(self, message: str, status_code: int, retry_after: int = 0):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after

def _sanitize_mentions(text: str) -> str:
    return re.sub(r'(?<!`)@(\w+)', r'`@\1`', text)

def _build_reconciliation_marker(finding_id: str, verification_id: str) -> str:
    return f"<!-- opencontrib:finding:{finding_id}:v:{verification_id} -->"

def _format_issue_body(finding: Finding, verification: Verification) -> str:
    evidence_text = ""
    for ev in finding.evidence:
        evidence_text += f"- `{ev.file}:{ev.line}`: `{ev.snippet}`\n"
        
    marker = _build_reconciliation_marker(finding.finding_id, verification.verification_id)
    
    body = f"""## Problem
{finding.description}

## Expected Behavior
{finding.expected_behavior}

## Evidence
{evidence_text}
## Why This May Be a Bug
(Auto-generated finding)

## Independent Verification
Status: {verification.status.value}
Reason: {verification.reason}

## Suggested Investigation
Please check the mentioned file and lines.

{marker}"""
    return body

async def _find_existing_issue_by_marker(finding: Finding, verification: Verification, github_client: IssueWriteClient, owner: str, repo_name: str) -> dict | None:
    marker = _build_reconciliation_marker(finding.finding_id, verification.verification_id)
    # Use exact match in body logic. The search query string syntax depends on client, assuming a generic search.
    query = f"repo:{owner}/{repo_name} {marker}"
    issues = await github_client.search_issues(owner, repo_name, query)
    for issue in issues:
        if marker in issue.get("body", ""):
            return issue
    return None

async def create_issue_if_authorized(
    gate_result: GateResult, 
    dedup_result: DedupResult, 
    finding: Finding, 
    verification: Verification, 
    github_client: IssueWriteClient, 
    owner: str, 
    repo_name: str
) -> IssueCreationResult:
    if gate_result.decision != GateDecision.ALLOW:
        raise Unauthorized(f"Gate denied issue creation: {gate_result.reason}")
        
    if dedup_result.finding_id != finding.finding_id:
        raise Unauthorized("Dedup reservation mismatch")
        
    existing_issue = await _find_existing_issue_by_marker(finding, verification, github_client, owner, repo_name)
    if existing_issue:
        return IssueCreationResult(
            issue_number=existing_issue["number"], 
            was_existing=True, 
            finding_id=finding.finding_id
        )
        
    body = _format_issue_body(finding, verification)
    sanitized_body = _sanitize_mentions(body)
    
    retries = 0
    max_retries = 2
    
    while True:
        try:
            issue = await github_client.create_issue(owner, repo_name, finding.title, sanitized_body, ["bug", finding.severity])
            return IssueCreationResult(
                issue_number=issue["number"], 
                was_existing=False, 
                finding_id=finding.finding_id
            )
        except TimeoutError:
            # On timeout: re-check marker -> if found return existing, else retry
            existing = await _find_existing_issue_by_marker(finding, verification, github_client, owner, repo_name)
            if existing:
                return IssueCreationResult(issue_number=existing["number"], was_existing=True, finding_id=finding.finding_id)
            if retries >= max_retries:
                raise IssueCreationError("Max retries exceeded on timeout")
            retries += 1
        except Exception as e:
            status_code = getattr(e, "status_code", 500)
            if status_code == 429:
                if retries >= 1:
                    raise IssueCreationError("Rate limit exceeded")
                retry_after = getattr(e, "retry_after", 1)
                await asyncio.sleep(retry_after)
                retries += 1
            elif 400 <= status_code < 500:
                raise IssueCreationError(f"Client error {status_code}: {e}")
            elif status_code >= 500:
                if retries >= max_retries:
                    raise IssueCreationError(f"Max retries exceeded on {status_code}")
                retries += 1
            else:
                raise IssueCreationError(f"Unexpected error: {e}")
