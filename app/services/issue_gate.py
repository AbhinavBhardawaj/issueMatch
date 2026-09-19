import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
import threading
from enum import Enum
from pydantic import BaseModel, ConfigDict
from app.domain.states import VerificationStatus, EvidenceStatus
from app.domain.models import Finding, Verification, RepoContext, ExistingIssue
from app.verifier.evidence import EvidenceValidationResult

# Dedup Logic

class DedupResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    signature: str
    finding_id: str
    is_duplicate: bool
    reason: str
    reservation_token: str = ""

STALE_RESERVATION_SECONDS: int = 300

def _normalize_description(description: str) -> str:
    desc = description[:120].lower()
    desc = re.sub(r'[^a-z0-9\s]', '', desc)
    desc = re.sub(r'\s+', ' ', desc).strip()
    return desc

def _normalize_text(text: str) -> str:
    t = text.lower()
    t = re.sub(r'[^a-z0-9\s]', '', t)
    return re.sub(r'\s+', ' ', t).strip()

def _normalize_snippet(snippet: str) -> str:
    # Normalize code snippet: strip lines, ignore blank lines, collapse whitespace
    lines = [line.strip() for line in snippet.strip().splitlines() if line.strip()]
    joined = " ".join(lines).lower()
    return re.sub(r'\s+', ' ', joined).strip()

def compute_defect_signature(
    repository_id: str,
    file: str,
    function: str | None = None,
    category: str = "",
    expected_behavior: str = "",
    evidence_snippet: str = "",
) -> str:
    """
    Stable deterministic defect signature based on structured identity:
    - repository_id
    - normalized file path
    - normalized function
    - normalized category
    - normalized expected_behavior
    - normalized primary evidence snippet
    Independent of finding_id, verification_id, commit_sha, line number, or free-form description.
    """
    payload = {
        "repository_id": str(repository_id).strip(),
        "file": file.strip().lower().replace("\\", "/"),
        "function": (function or "").strip().lower(),
        "category": (category or "").strip().lower(),
        "expected_behavior": _normalize_text(expected_behavior),
        "evidence_snippet": _normalize_snippet(evidence_snippet),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

def compute_finding_signature(finding: Finding) -> str:
    primary_snippet = finding.evidence[0].snippet if finding.evidence else ""
    return compute_defect_signature(
        repository_id=finding.repository_id,
        file=finding.file,
        function=finding.function,
        category=getattr(finding, "category", ""),
        expected_behavior=getattr(finding, "expected_behavior", ""),
        evidence_snippet=primary_snippet,
    )

def normalized_finding_signature(
    repository_id: str | Finding,
    file: str | None = None,
    function: str | None = None,
    description: str = "",
    category: str = "",
    expected_behavior: str = "",
    evidence_snippet: str = "",
) -> str:
    if isinstance(repository_id, Finding):
        return compute_finding_signature(repository_id)
    if category or expected_behavior or evidence_snippet:
        return compute_defect_signature(
            repository_id=repository_id,
            file=file or "",
            function=function,
            category=category,
            expected_behavior=expected_behavior,
            evidence_snippet=evidence_snippet,
        )
    # Legacy fallback for callers passing (repo, file, func, desc)
    normalized = _normalize_description(description)
    payload = json.dumps({
        "repository_id": str(repository_id),
        "file": (file or "").strip().lower(),
        "function": (function or "").strip().lower(),
        "defect": normalized
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()

class InMemoryDedupStore:
    def __init__(self):
        self._reservations: dict[str, dict] = {}
        self._lock = threading.Lock()

    def check_and_reserve(self, finding: Finding) -> DedupResult:
        signature = compute_finding_signature(finding)
        now = datetime.now(timezone.utc)

        with self._lock:
            if signature in self._reservations:
                reservation = self._reservations[signature]
                if reservation["finding_id"] == finding.finding_id:
                    return DedupResult(
                        signature=signature,
                        finding_id=finding.finding_id,
                        is_duplicate=False,
                        reason="Own reservation",
                        reservation_token=reservation.get("reservation_token", ""),
                    )

                age_seconds = (now - reservation["timestamp"]).total_seconds()
                if age_seconds > STALE_RESERVATION_SECONDS:
                    token = secrets.token_hex(32)
                    self._reservations[signature] = {
                        "finding_id": finding.finding_id,
                        "timestamp": now,
                        "reservation_token": token,
                    }
                    return DedupResult(
                        signature=signature,
                        finding_id=finding.finding_id,
                        is_duplicate=False,
                        reason="Overwrote stale reservation",
                        reservation_token=token,
                    )

                return DedupResult(
                    signature=signature,
                    finding_id=reservation["finding_id"],
                    is_duplicate=True,
                    reason="Active reservation exists",
                    reservation_token="",
                )

            token = secrets.token_hex(32)
            self._reservations[signature] = {
                "finding_id": finding.finding_id,
                "timestamp": now,
                "reservation_token": token,
            }
            return DedupResult(
                signature=signature,
                finding_id=finding.finding_id,
                is_duplicate=False,
                reason="New reservation",
                reservation_token=token,
            )

    def validate_reservation(self, finding: Finding, dedup_result: DedupResult) -> bool:
        """
        Thread-safe validation confirming reservation authenticity:
        - non-empty reservation_token
        - signature matches compute_finding_signature(finding)
        - signature exists in store
        - finding_id matches
        - reservation_token matches stored token (constant time)
        - reservation has not expired / been replaced
        """
        if not dedup_result.reservation_token:
            return False

        expected_signature = compute_finding_signature(finding)
        if dedup_result.signature != expected_signature:
            return False

        with self._lock:
            stored = self._reservations.get(dedup_result.signature)
            if not stored:
                return False
            if str(stored.get("finding_id")) != str(finding.finding_id):
                return False
            stored_token = stored.get("reservation_token", "")
            if not secrets.compare_digest(stored_token, dedup_result.reservation_token):
                return False
            now = datetime.now(timezone.utc)
            if (now - stored["timestamp"]).total_seconds() > STALE_RESERVATION_SECONDS:
                return False
            return True

def check_existing_github_issues(finding: Finding, existing_issues: list[ExistingIssue]) -> bool:
    marker = f"opencontrib:finding:{finding.finding_id}"
    sig = compute_finding_signature(finding)
    sig_marker = f"opencontrib:signature:{sig}"
    for issue in existing_issues:
        body = issue.body_summary or ""
        if marker in body or sig_marker in body:
            return True
    return False

# Gate Logic

class GateDecision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"

class GateResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: GateDecision
    reason: str

def evaluate_issue_authorization(
    finding: Finding,
    verification: Verification,
    evidence_result: EvidenceValidationResult,
    repo_context: RepoContext,
    dedup_result: DedupResult
) -> GateResult:
    """
    Pure deterministic authorization policy.
    Evaluated by VerifierPipeline and re-evaluated by IssueCreator immediately before write.
    """
    if verification.status != VerificationStatus.VERIFIED:
        return GateResult(decision=GateDecision.DENY, reason="VERIFIER_NOT_VERIFIED")

    if str(verification.finding_id) != str(finding.finding_id):
        return GateResult(decision=GateDecision.DENY, reason="FINDING_ID_MISMATCH")

    if str(verification.installation_id) != str(finding.installation_id):
        return GateResult(decision=GateDecision.DENY, reason="INSTALLATION_MISMATCH")

    if str(verification.repository_id) != str(finding.repository_id):
        return GateResult(decision=GateDecision.DENY, reason="REPOSITORY_MISMATCH")

    if str(verification.commit_sha) != str(finding.commit_sha):
        return GateResult(decision=GateDecision.DENY, reason="COMMIT_SHA_MISMATCH")

    if str(repo_context.installation_id) != str(finding.installation_id):
        return GateResult(decision=GateDecision.DENY, reason="REPO_CONTEXT_INSTALLATION_MISMATCH")

    if str(repo_context.repository_id) != str(finding.repository_id):
        return GateResult(decision=GateDecision.DENY, reason="REPO_CONTEXT_REPOSITORY_MISMATCH")

    if str(repo_context.commit_sha) != str(finding.commit_sha):
        return GateResult(decision=GateDecision.DENY, reason="REPO_CONTEXT_COMMIT_SHA_MISMATCH")

    if str(evidence_result.finding_id) != str(finding.finding_id):
        return GateResult(decision=GateDecision.DENY, reason="EVIDENCE_FINDING_ID_MISMATCH")

    if str(evidence_result.commit_sha) != str(finding.commit_sha):
        return GateResult(decision=GateDecision.DENY, reason="EVIDENCE_COMMIT_SHA_MISMATCH")

    if len(finding.evidence) == 0:
        return GateResult(decision=GateDecision.DENY, reason="NO_EVIDENCE")

    if evidence_result.overall != EvidenceStatus.SUPPORTED:
        return GateResult(decision=GateDecision.DENY, reason="EVIDENCE_NOT_SUPPORTED")

    if len(evidence_result.results) == 0:
        return GateResult(decision=GateDecision.DENY, reason="EVIDENCE_COVERAGE_MISMATCH")

    for item in evidence_result.results:
        if item.status == EvidenceStatus.CONTRADICTED:
            return GateResult(decision=GateDecision.DENY, reason="EVIDENCE_CONTRADICTED")
        if item.status == EvidenceStatus.UNKNOWN:
            return GateResult(decision=GateDecision.DENY, reason="EVIDENCE_UNKNOWN")
        if item.status != EvidenceStatus.SUPPORTED:
            return GateResult(decision=GateDecision.DENY, reason="EVIDENCE_NOT_SUPPORTED")

    def _norm_path(p: str) -> str:
        return p.strip().lower().replace("\\", "/")

    def _norm_snip(s: str) -> str:
        return _normalize_snippet(s)

    for ev_item in finding.evidence:
        ev_file = _norm_path(ev_item.file)
        ev_line = int(ev_item.line)
        ev_snippet = _norm_snip(ev_item.snippet)

        matched = False
        for res_item in evidence_result.results:
            if (
                _norm_path(res_item.file) == ev_file
                and int(res_item.line) == ev_line
                and _norm_snip(res_item.snippet) == ev_snippet
                and res_item.status == EvidenceStatus.SUPPORTED
            ):
                matched = True
                break
        if not matched:
            return GateResult(decision=GateDecision.DENY, reason="EVIDENCE_COVERAGE_MISMATCH")

    if dedup_result.is_duplicate:
        return GateResult(decision=GateDecision.DENY, reason="DUPLICATE")

    if str(dedup_result.finding_id) != str(finding.finding_id):
        return GateResult(decision=GateDecision.DENY, reason="DEDUP_RESERVATION_MISMATCH")

    expected_signature = compute_finding_signature(finding)
    if dedup_result.signature != expected_signature:
        return GateResult(decision=GateDecision.DENY, reason="SIGNATURE_MISMATCH")

    if verification.duplicate_issue or check_existing_github_issues(finding, repo_context.existing_issues):
        return GateResult(decision=GateDecision.DENY, reason="EXISTING_GITHUB_ISSUE")

    # Security findings publication guard: do not publish public GitHub issues for security category
    # Must be held for manual review
    finding_category = (getattr(finding, "category", "") or getattr(finding, "severity", "")).lower()
    if finding_category == "security":
        return GateResult(decision=GateDecision.DENY, reason="SECURITY_MANUAL_REVIEW_REQUIRED")

    return GateResult(decision=GateDecision.ALLOW, reason="ALL_CONDITIONS_MET")

def should_create_issue(
    finding: Finding,
    verification: Verification,
    evidence_result: EvidenceValidationResult,
    repo_context: RepoContext,
    dedup_result: DedupResult
) -> GateResult:
    return evaluate_issue_authorization(finding, verification, evidence_result, repo_context, dedup_result)
