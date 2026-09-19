import re
import asyncio
import logging
from typing import Protocol
from pydantic import BaseModel, ConfigDict
from app.domain.states import EvidenceStatus
from app.domain.models import Finding, Verification, RepoContext
from app.verifier.evidence import EvidenceValidationResult, EvidenceItemResult
from app.services.issue_gate import (
    GateResult,
    GateDecision,
    DedupResult,
    DedupStore,
    evaluate_issue_authorization,
)
from app.services.write_journal import (
    IssueWriteJournal,
    InMemoryIssueWriteJournal,
    IntentState,
    UnresolvedWriteIntentError,
)
from app.github.client import (
    GitHubClientError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubAuthenticationError,
    GitHubServerError,
    GitHubTransportError,
    GitHubRequestTimeoutError,
    GitHubAmbiguousWriteError,
)

logger = logging.getLogger(__name__)


class IssueWriteClient(Protocol):
    async def create_issue(self, owner: str, repo: str, title: str, body: str, labels: list[str] | None = None) -> dict: ...
    async def search_issues(self, owner: str, repo: str, query: str) -> list[dict]: ...
    async def get_issues(self, owner: str, repo: str, state: str = "all", per_page: int = 30) -> list[dict]: ...
    async def get_repository(self, owner: str, repo: str) -> dict: ...


class IssueCreationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    issue_number: int
    was_existing: bool
    finding_id: str


class IssueCreationError(Exception):
    pass


class Unauthorized(IssueCreationError):
    pass


class ReconciliationUnavailableError(IssueCreationError):
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


def _build_signature_marker(signature: str) -> str:
    return f"<!-- opencontrib:signature:{signature} -->"


def _format_issue_body(finding: Finding, verification: Verification, signature: str = "") -> str:
    evidence_text = ""
    for ev in finding.evidence:
        evidence_text += f"- `{ev.file}:{ev.line}`: `{ev.snippet}`\n"

    marker = _build_reconciliation_marker(finding.finding_id, verification.verification_id)
    sig_marker = _build_signature_marker(signature) if signature else ""

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

{marker}
{sig_marker}"""
    return body


async def reconcile_existing_issue(
    finding: Finding,
    verification: Verification,
    github_client: IssueWriteClient,
    owner: str,
    repo_name: str,
    signature: str = "",
) -> dict | None:
    sig_marker = _build_signature_marker(signature) if signature else ""
    finding_marker = _build_reconciliation_marker(finding.finding_id, verification.verification_id)

    search_ok = False
    search_err = None

    # 1. Primary reconciliation: search issues (by signature if present, else by finding marker)
    query = f"opencontrib:signature:{signature}" if signature else f"opencontrib:finding:{finding.finding_id}"
    try:
        issues = await github_client.search_issues(owner, repo_name, query)
        search_ok = True
        for issue in issues:
            body = issue.get("body", "")
            if sig_marker and sig_marker in body:
                return issue
            if finding_marker and finding_marker in body:
                return issue
    except Exception as exc:
        search_err = exc
        logger.warning(f"Search issues failed: {exc}")

    # 2. Bounded fallback: inspect recent issues directly to handle search index delay
    recent_ok = False
    recent_err = None
    has_recent_capability = hasattr(github_client, "get_issues")

    if has_recent_capability:
        try:
            recent_issues = await github_client.get_issues(owner, repo_name, state="all", per_page=30)
            recent_ok = True
            for issue in recent_issues:
                body = issue.get("body", "")
                if sig_marker and sig_marker in body:
                    return issue
                if finding_marker and finding_marker in body:
                    return issue
        except Exception as exc:
            recent_err = exc
            logger.warning(f"Recent issues fallback failed: {exc}")

    # If both reconciliation mechanisms failed operationally (or search failed and no get_issues supported):
    if not search_ok and (not has_recent_capability or not recent_ok):
        err_msg = f"Reconciliation unavailable: search error ({search_err})"
        if recent_err:
            err_msg += f", get_issues error ({recent_err})"
        raise ReconciliationUnavailableError(err_msg)

    # At least one mechanism succeeded and confirmed no matching issue exists
    return None


find_existing_issue = reconcile_existing_issue
_find_existing_issue_by_marker = reconcile_existing_issue


async def create_issue_if_authorized(
    gate_result: GateResult,
    dedup_result: DedupResult,
    finding: Finding,
    verification: Verification,
    github_client: IssueWriteClient,
    owner: str,
    repo_name: str,
    *,
    evidence_result: EvidenceValidationResult,
    repo_context: RepoContext,
    dedup_store: DedupStore,
    write_journal: IssueWriteJournal | None = None,
) -> IssueCreationResult:
    # 0. Reject caller-supplied DENY immediately
    if gate_result.decision != GateDecision.ALLOW:
        raise Unauthorized(f"Gate denied issue creation: {gate_result.reason}")

    # 1. Require genuine non-null evidence_result and repo_context
    if evidence_result is None or repo_context is None:
        raise Unauthorized("Valid evidence_result and repo_context are mandatory for write authorization")

    # 2. Write destination binding: actual owner/name must match repo_context
    if (
        owner.strip().lower() != repo_context.owner.strip().lower()
        or repo_name.strip().lower() != repo_context.name.strip().lower()
    ):
        raise Unauthorized(
            f"Write destination mismatch: argument '{owner}/{repo_name}' does not match "
            f"repo_context '{repo_context.owner}/{repo_context.name}'"
        )

    # 3. Live numeric GitHub repository identity check
    try:
        repo_metadata = await github_client.get_repository(owner, repo_name)
    except Exception as exc:
        raise Unauthorized(f"Failed to verify target repository metadata: {exc}") from exc

    if not isinstance(repo_metadata, dict) or "id" not in repo_metadata:
        raise Unauthorized("Malformed repository metadata from GitHub API")

    if str(repo_metadata["id"]) != str(finding.repository_id):
        raise Unauthorized(
            f"Target repository ID mismatch: GitHub returned {repo_metadata.get('id')} "
            f"but finding expects {finding.repository_id}"
        )

    # 4. Authentic Dedup reservation validation (asynchronous)
    if dedup_store is None or not await dedup_store.validate_reservation(finding, dedup_result):
        raise Unauthorized("Dedup reservation is invalid, expired, or forged")

    # 5. Defense-in-depth: Re-evaluate pure deterministic authorization policy
    auth_gate = evaluate_issue_authorization(
        finding=finding,
        verification=verification,
        evidence_result=evidence_result,
        repo_context=repo_context,
        dedup_result=dedup_result,
    )
    if auth_gate.decision != GateDecision.ALLOW:
        raise Unauthorized(f"Deterministic authorization failed: {auth_gate.reason}")

    # Fallback to in-memory journal if none provided
    journal = write_journal or InMemoryIssueWriteJournal()

    # 6. Check existing write intent for this signature
    signature = dedup_result.signature
    existing_intent = await journal.get_intent(signature)
    if existing_intent:
        intent_state = existing_intent.get("state")
        if intent_state == IntentState.COMMITTED.value:
            issue_num = existing_intent.get("issue_number", 0)
            await dedup_store.commit_reservation(signature, str(finding.finding_id), issue_num)
            return IssueCreationResult(
                issue_number=issue_num,
                was_existing=True,
                finding_id=str(finding.finding_id),
            )
        elif intent_state == IntentState.POSTING.value:
            # Ambiguous state after previous crash: enter RECONCILIATION-ONLY mode
            logger.warning(f"Unresolved write intent in POSTING state for signature {signature}; entering reconciliation-only mode")
            existing = await reconcile_existing_issue(
                finding, verification, github_client, owner, repo_name, signature=signature
            )
            if existing:
                await journal.commit_intent(signature, existing_intent["intent_id"], existing["number"])
                await dedup_store.commit_reservation(signature, str(finding.finding_id), existing["number"])
                return IssueCreationResult(
                    issue_number=existing["number"],
                    was_existing=True,
                    finding_id=str(finding.finding_id),
                )
            # Not found: fail closed to prevent creating a duplicate
            raise UnresolvedWriteIntentError(
                f"Write intent in POSTING state could not be confirmed on GitHub for signature {signature}. "
                "Refusing blind re-POST to prevent duplicate issue creation. Manual verification required."
            )

    # 7. Pre-write reconciliation: check if issue already exists before writing
    existing_issue = await reconcile_existing_issue(
        finding, verification, github_client, owner, repo_name, signature=signature
    )
    if existing_issue:
        await dedup_store.commit_reservation(signature, str(finding.finding_id), existing_issue["number"])
        return IssueCreationResult(
            issue_number=existing_issue["number"],
            was_existing=True,
            finding_id=str(finding.finding_id),
        )

    # 8. Persist ISSUE_WRITE_INTENT and atomically transition dedup reservation to WRITE_PENDING
    intent_id = await journal.create_write_intent(
        finding=finding,
        verification=verification,
        repo_context=repo_context,
        signature=signature,
        reservation_token=dedup_result.reservation_token,
    )

    # 9. Revalidate reservation immediately before POST attempt
    if not await dedup_store.validate_reservation(finding, dedup_result):
        await journal.abort_intent(signature, intent_id)
        raise Unauthorized("Dedup reservation expired or was invalidated immediately before POST")

    # 10. Transition intent state from PREPARED to POSTING
    await journal.transition_to_posting(signature, intent_id)

    body = _format_issue_body(finding, verification, signature=signature)
    sanitized_body = _sanitize_mentions(body)

    retries = 0
    max_retries = 2

    while True:
        try:
            issue = await github_client.create_issue(
                owner, repo_name, finding.title, sanitized_body, ["bug", finding.severity]
            )
            await journal.commit_intent(signature, intent_id, issue["number"])
            await dedup_store.commit_reservation(signature, str(finding.finding_id), issue["number"])
            return IssueCreationResult(
                issue_number=issue["number"],
                was_existing=False,
                finding_id=str(finding.finding_id),
            )
        except (GitHubAmbiguousWriteError, GitHubRequestTimeoutError, TimeoutError) as exc:
            # Remote issue write status is ambiguous: reconcile immediately
            existing = await reconcile_existing_issue(
                finding, verification, github_client, owner, repo_name, signature=signature
            )
            if existing:
                await journal.commit_intent(signature, intent_id, existing["number"])
                await dedup_store.commit_reservation(signature, str(finding.finding_id), existing["number"])
                return IssueCreationResult(
                    issue_number=existing["number"],
                    was_existing=True,
                    finding_id=str(finding.finding_id),
                )
            if retries >= max_retries:
                raise IssueCreationError(f"Max retries exceeded on ambiguous write timeout: {exc}")
            retries += 1

            # Before attempting another POST retry: reconcile again
            existing_before_retry = await reconcile_existing_issue(
                finding, verification, github_client, owner, repo_name, signature=signature
            )
            if existing_before_retry:
                await journal.commit_intent(signature, intent_id, existing_before_retry["number"])
                await dedup_store.commit_reservation(signature, str(finding.finding_id), existing_before_retry["number"])
                return IssueCreationResult(
                    issue_number=existing_before_retry["number"],
                    was_existing=True,
                    finding_id=str(finding.finding_id),
                )
        except (GitHubAuthenticationError, Unauthorized) as exc:
            await journal.abort_intent(signature, intent_id)
            raise IssueCreationError(f"GitHub authentication error: {exc}")
        except GitHubRateLimitError as exc:
            if retries >= 1:
                raise IssueCreationError(f"GitHub rate limit exceeded: {exc}")
            retry_after = getattr(exc, "retry_after", 1)
            await asyncio.sleep(min(retry_after, 5))
            retries += 1
        except GitHubServerError as exc:
            if retries >= max_retries:
                raise IssueCreationError(f"Max retries exceeded on GitHub server error: {exc}")
            retries += 1
        except GitHubClientError as exc:
            status_code = getattr(exc, "status_code", 400)
            if status_code in (401, 403):
                await journal.abort_intent(signature, intent_id)
                raise IssueCreationError(f"Authentication error {status_code}: {exc}")
            elif status_code == 429:
                if retries >= 1:
                    raise IssueCreationError("Rate limit exceeded")
                retry_after = getattr(exc, "retry_after", 1)
                await asyncio.sleep(min(retry_after, 5))
                retries += 1
            elif 400 <= status_code < 500:
                await journal.abort_intent(signature, intent_id)
                raise IssueCreationError(f"Client error {status_code}: {exc}")
            elif status_code >= 500:
                if retries >= max_retries:
                    raise IssueCreationError(f"Max retries exceeded on {status_code}: {exc}")
                retries += 1
            else:
                raise IssueCreationError(f"GitHub client error: {exc}")
        except GitHubAPIError as exc:
            if exc.status_code in (401, 403):
                await journal.abort_intent(signature, intent_id)
                raise IssueCreationError(f"Authentication failure: {exc}")
            elif exc.status_code == 429:
                if retries >= 1:
                    raise IssueCreationError("Rate limit exceeded")
                await asyncio.sleep(min(exc.retry_after, 5))
                retries += 1
            elif 400 <= exc.status_code < 500:
                await journal.abort_intent(signature, intent_id)
                raise IssueCreationError(f"Client error {exc.status_code}: {exc}")
            elif exc.status_code >= 500:
                if retries >= max_retries:
                    raise IssueCreationError(f"Max retries exceeded on {exc.status_code}")
                retries += 1
            else:
                raise IssueCreationError(f"API error: {exc}")
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            if status_code in (401, 403):
                await journal.abort_intent(signature, intent_id)
                raise IssueCreationError(f"Authentication error {status_code}: {exc}")
            elif status_code == 429:
                if retries >= 1:
                    raise IssueCreationError("Rate limit exceeded")
                retry_after = getattr(exc, "retry_after", 1)
                await asyncio.sleep(min(retry_after, 5))
                retries += 1
            elif status_code and 400 <= status_code < 500:
                await journal.abort_intent(signature, intent_id)
                raise IssueCreationError(f"Client error {status_code}: {exc}")
            elif status_code and status_code >= 500:
                if retries >= max_retries:
                    raise IssueCreationError(f"Max retries exceeded on {status_code}")
                retries += 1
            else:
                raise IssueCreationError(f"Unexpected error: {exc}")
