import hashlib
import json
import re
import secrets
import time
from datetime import datetime, timezone
import asyncio
from enum import Enum
from typing import Protocol
from pydantic import BaseModel, ConfigDict
from app.domain.states import VerificationStatus, EvidenceStatus
from app.domain.models import Finding, Verification, RepoContext, ExistingIssue, ContextCompleteness
from app.domain.normalization import normalize_code, normalize_path
from app.verifier.evidence import EvidenceValidationResult
from app.verifier.citation_validator import validate_verifier_citations
from app.storage.sqlite import get_db_connection, init_schema


class DedupState(str, Enum):
    RESERVED = "RESERVED"
    WRITE_PENDING = "WRITE_PENDING"
    COMMITTED = "COMMITTED"


class DedupResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    signature: str
    finding_id: str
    is_duplicate: bool
    reason: str
    reservation_token: str = ""


DEFAULT_RESERVATION_TTL: float = 300.0
STALE_RESERVATION_SECONDS: float = DEFAULT_RESERVATION_TTL


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
    **kwargs,
) -> str:
    payload = {
        "repository_id": str(repository_id).strip(),
        "file": normalize_path(file),
        "function": (function or "").strip(),
        "category": (category or "").strip().lower(),
        "evidence_snippet": normalize_code(evidence_snippet),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def compute_finding_signature(finding: Finding) -> str:
    primary_snippet = finding.evidence[0].snippet if finding.evidence else ""
    return compute_defect_signature(
        repository_id=finding.repository_id,
        file=finding.file,
        function=finding.function,
        category=getattr(finding, "category", ""),
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
    normalized = _normalize_description(description)
    payload = json.dumps({
        "repository_id": str(repository_id),
        "file": (file or "").strip().lower(),
        "function": (function or "").strip().lower(),
        "defect": normalized
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


class DedupStore(Protocol):
    async def check_and_reserve(self, finding: Finding) -> DedupResult: ...
    async def validate_reservation(self, finding: Finding, dedup_result: DedupResult) -> bool: ...
    async def renew_reservation(self, finding: Finding, reservation_token: str, additional_seconds: float = 300.0) -> bool: ...
    async def transition_to_write_pending(self, signature: str, finding_id: str, reservation_token: str) -> bool: ...
    async def commit_reservation(self, signature: str, finding_id: str, issue_number: int) -> bool: ...
    async def get_reservation(self, signature: str) -> dict | None: ...


class InMemoryDedupStore:
    def __init__(self, reservation_ttl: float = DEFAULT_RESERVATION_TTL):
        self.reservation_ttl = reservation_ttl
        self._reservations: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def check_and_reserve(self, finding: Finding) -> DedupResult:
        signature = compute_finding_signature(finding)
        now = time.time()

        async with self._lock:
            if signature in self._reservations:
                reservation = self._reservations[signature]
                state = reservation.get("state", DedupState.RESERVED.value)

                if state == DedupState.COMMITTED.value:
                    return DedupResult(
                        signature=signature,
                        finding_id=reservation["finding_id"],
                        is_duplicate=True,
                        reason=f"Already committed as issue #{reservation.get('issue_number')}",
                        reservation_token="",
                    )

                if state == DedupState.WRITE_PENDING.value:
                    if reservation["finding_id"] == finding.finding_id:
                        return DedupResult(
                            signature=signature,
                            finding_id=finding.finding_id,
                            is_duplicate=False,
                            reason="Own write-pending reservation",
                            reservation_token=reservation.get("reservation_token", ""),
                        )
                    return DedupResult(
                        signature=signature,
                        finding_id=reservation["finding_id"],
                        is_duplicate=True,
                        reason="Issue write in progress by another worker",
                        reservation_token="",
                    )

                # state == RESERVED
                if reservation["finding_id"] == finding.finding_id:
                    if reservation["lease_expires_at"] > now:
                        return DedupResult(
                            signature=signature,
                            finding_id=finding.finding_id,
                            is_duplicate=False,
                            reason="Own reservation",
                            reservation_token=reservation.get("reservation_token", ""),
                        )
                    token = secrets.token_hex(32)
                    reservation["reservation_token"] = token
                    reservation["lease_expires_at"] = now + self.reservation_ttl
                    reservation["updated_at"] = now
                    return DedupResult(
                        signature=signature,
                        finding_id=finding.finding_id,
                        is_duplicate=False,
                        reason="Renewed expired own reservation",
                        reservation_token=token,
                    )

                # Different finding_id
                if reservation["lease_expires_at"] > now:
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
                    "reservation_token": token,
                    "state": DedupState.RESERVED.value,
                    "issue_number": None,
                    "created_at": now,
                    "lease_expires_at": now + self.reservation_ttl,
                    "updated_at": now,
                }
                return DedupResult(
                    signature=signature,
                    finding_id=finding.finding_id,
                    is_duplicate=False,
                    reason="Overwrote stale reservation",
                    reservation_token=token,
                )

            token = secrets.token_hex(32)
            self._reservations[signature] = {
                "finding_id": finding.finding_id,
                "reservation_token": token,
                "state": DedupState.RESERVED.value,
                "issue_number": None,
                "created_at": now,
                "lease_expires_at": now + self.reservation_ttl,
                "updated_at": now,
            }
            return DedupResult(
                signature=signature,
                finding_id=finding.finding_id,
                is_duplicate=False,
                reason="New reservation",
                reservation_token=token,
            )

    async def validate_reservation(self, finding: Finding, dedup_result: DedupResult) -> bool:
        if not dedup_result.reservation_token:
            return False

        expected_signature = compute_finding_signature(finding)
        if dedup_result.signature != expected_signature:
            return False

        async with self._lock:
            stored = self._reservations.get(dedup_result.signature)
            if not stored:
                return False
            if str(stored.get("finding_id")) != str(finding.finding_id):
                return False
            stored_token = stored.get("reservation_token", "")
            if not secrets.compare_digest(stored_token, dedup_result.reservation_token):
                return False
            state = stored.get("state", DedupState.RESERVED.value)
            if state not in (DedupState.RESERVED.value, DedupState.WRITE_PENDING.value):
                return False
            if state == DedupState.RESERVED.value and stored.get("lease_expires_at", 0) <= time.time():
                return False
            return True

    async def renew_reservation(self, finding: Finding, reservation_token: str, additional_seconds: float = 300.0) -> bool:
        expected_signature = compute_finding_signature(finding)
        async with self._lock:
            stored = self._reservations.get(expected_signature)
            if not stored:
                return False
            if stored.get("finding_id") != finding.finding_id:
                return False
            if not secrets.compare_digest(stored.get("reservation_token", ""), reservation_token):
                return False
            now = time.time()
            stored["lease_expires_at"] = max(stored.get("lease_expires_at", 0), now) + additional_seconds
            stored["updated_at"] = now
            return True

    async def transition_to_write_pending(self, signature: str, finding_id: str, reservation_token: str) -> bool:
        now = time.time()
        async with self._lock:
            stored = self._reservations.get(signature)
            if not stored:
                return False
            if stored.get("finding_id") != finding_id:
                return False
            if not secrets.compare_digest(stored.get("reservation_token", ""), reservation_token):
                return False
            if stored.get("state") != DedupState.RESERVED.value:
                return False
            if stored.get("lease_expires_at", 0) <= now:
                return False
            stored["state"] = DedupState.WRITE_PENDING.value
            stored["updated_at"] = now
            return True

    async def commit_reservation(self, signature: str, finding_id: str, issue_number: int) -> bool:
        now = time.time()
        async with self._lock:
            stored = self._reservations.get(signature)
            if stored:
                stored["state"] = DedupState.COMMITTED.value
                stored["issue_number"] = issue_number
                stored["updated_at"] = now
                return True
            self._reservations[signature] = {
                "finding_id": finding_id,
                "reservation_token": "",
                "state": DedupState.COMMITTED.value,
                "issue_number": issue_number,
                "created_at": now,
                "lease_expires_at": float("inf"),
                "updated_at": now,
            }
            return True

    async def get_reservation(self, signature: str) -> dict | None:
        async with self._lock:
            stored = self._reservations.get(signature)
            return dict(stored) if stored else None


class SQLiteDedupStore:
    def __init__(self, db_path: str, reservation_ttl: float = DEFAULT_RESERVATION_TTL):
        self.db_path = db_path
        self.reservation_ttl = reservation_ttl
        self._initialized = False

    async def _ensure_init(self) -> None:
        if not self._initialized:
            await init_schema(self.db_path)
            self._initialized = True

    async def check_and_reserve(self, finding: Finding) -> DedupResult:
        await self._ensure_init()
        signature = compute_finding_signature(finding)
        now = time.time()

        async with await get_db_connection(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE;")
            async with db.execute(
                "SELECT finding_id, reservation_token, state, issue_number, lease_expires_at FROM dedup_reservations WHERE signature = ?;",
                (signature,),
            ) as cursor:
                row = await cursor.fetchone()

            if row:
                state = row["state"]
                row_finding_id = row["finding_id"]
                token = row["reservation_token"]
                issue_number = row["issue_number"]
                lease_expires_at = row["lease_expires_at"]

                if state == DedupState.COMMITTED.value:
                    await db.rollback()
                    return DedupResult(
                        signature=signature,
                        finding_id=row_finding_id,
                        is_duplicate=True,
                        reason=f"Already committed as issue #{issue_number}",
                        reservation_token="",
                    )

                if state == DedupState.WRITE_PENDING.value:
                    await db.rollback()
                    if row_finding_id == finding.finding_id:
                        return DedupResult(
                            signature=signature,
                            finding_id=finding.finding_id,
                            is_duplicate=False,
                            reason="Own write-pending reservation",
                            reservation_token=token,
                        )
                    return DedupResult(
                        signature=signature,
                        finding_id=row_finding_id,
                        is_duplicate=True,
                        reason="Issue write in progress by another worker",
                        reservation_token="",
                    )

                # state == RESERVED
                if row_finding_id == finding.finding_id:
                    if lease_expires_at > now:
                        await db.rollback()
                        return DedupResult(
                            signature=signature,
                            finding_id=finding.finding_id,
                            is_duplicate=False,
                            reason="Own reservation",
                            reservation_token=token,
                        )
                    new_token = secrets.token_hex(32)
                    new_lease = now + self.reservation_ttl
                    await db.execute(
                        "UPDATE dedup_reservations SET reservation_token = ?, lease_expires_at = ?, updated_at = ? WHERE signature = ?;",
                        (new_token, new_lease, now, signature),
                    )
                    await db.commit()
                    return DedupResult(
                        signature=signature,
                        finding_id=finding.finding_id,
                        is_duplicate=False,
                        reason="Renewed expired own reservation",
                        reservation_token=new_token,
                    )

                # Different finding_id
                if lease_expires_at > now:
                    await db.rollback()
                    return DedupResult(
                        signature=signature,
                        finding_id=row_finding_id,
                        is_duplicate=True,
                        reason="Active reservation exists",
                        reservation_token="",
                    )

                new_token = secrets.token_hex(32)
                new_lease = now + self.reservation_ttl
                await db.execute(
                    "UPDATE dedup_reservations SET finding_id = ?, reservation_token = ?, lease_expires_at = ?, updated_at = ? WHERE signature = ?;",
                    (finding.finding_id, new_token, new_lease, now, signature),
                )
                await db.commit()
                return DedupResult(
                    signature=signature,
                    finding_id=finding.finding_id,
                    is_duplicate=False,
                    reason="Overwrote stale reservation",
                    reservation_token=new_token,
                )

            new_token = secrets.token_hex(32)
            new_lease = now + self.reservation_ttl
            await db.execute(
                """
                INSERT INTO dedup_reservations (
                    signature, finding_id, reservation_token, state, issue_number,
                    created_at, lease_expires_at, updated_at
                ) VALUES (?, ?, ?, 'RESERVED', NULL, ?, ?, ?);
                """,
                (signature, finding.finding_id, new_token, now, new_lease, now),
            )
            await db.commit()
            return DedupResult(
                signature=signature,
                finding_id=finding.finding_id,
                is_duplicate=False,
                reason="New reservation",
                reservation_token=new_token,
            )

    async def validate_reservation(self, finding: Finding, dedup_result: DedupResult) -> bool:
        if not dedup_result.reservation_token:
            return False

        expected_signature = compute_finding_signature(finding)
        if dedup_result.signature != expected_signature:
            return False

        await self._ensure_init()
        now = time.time()
        async with await get_db_connection(self.db_path) as db:
            async with db.execute(
                "SELECT finding_id, reservation_token, state, lease_expires_at FROM dedup_reservations WHERE signature = ?;",
                (dedup_result.signature,),
            ) as cursor:
                row = await cursor.fetchone()

            if not row:
                return False
            if str(row["finding_id"]) != str(finding.finding_id):
                return False
            if not secrets.compare_digest(row["reservation_token"], dedup_result.reservation_token):
                return False
            state = row["state"]
            if state not in (DedupState.RESERVED.value, DedupState.WRITE_PENDING.value):
                return False
            if state == DedupState.RESERVED.value and row["lease_expires_at"] <= now:
                return False
            return True

    async def renew_reservation(self, finding: Finding, reservation_token: str, additional_seconds: float = 300.0) -> bool:
        await self._ensure_init()
        signature = compute_finding_signature(finding)
        now = time.time()
        async with await get_db_connection(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE;")
            cursor = await db.execute(
                """
                UPDATE dedup_reservations
                SET lease_expires_at = MAX(lease_expires_at, ?) + ?, updated_at = ?
                WHERE signature = ? AND finding_id = ? AND reservation_token = ? AND state = 'RESERVED';
                """,
                (now, additional_seconds, now, signature, finding.finding_id, reservation_token),
            )
            updated = cursor.rowcount > 0
            if updated:
                await db.commit()
            else:
                await db.rollback()
            return updated

    async def transition_to_write_pending(self, signature: str, finding_id: str, reservation_token: str) -> bool:
        await self._ensure_init()
        now = time.time()
        async with await get_db_connection(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE;")
            cursor = await db.execute(
                """
                UPDATE dedup_reservations
                SET state = 'WRITE_PENDING', updated_at = ?
                WHERE signature = ? AND finding_id = ? AND reservation_token = ? AND state = 'RESERVED' AND lease_expires_at > ?;
                """,
                (now, signature, finding_id, reservation_token, now),
            )
            updated = cursor.rowcount > 0
            if updated:
                await db.commit()
            else:
                await db.rollback()
            return updated

    async def commit_reservation(self, signature: str, finding_id: str, issue_number: int) -> bool:
        await self._ensure_init()
        now = time.time()
        async with await get_db_connection(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE;")
            cursor = await db.execute(
                """
                UPDATE dedup_reservations
                SET state = 'COMMITTED', issue_number = ?, updated_at = ?
                WHERE signature = ?;
                """,
                (issue_number, now, signature),
            )
            if cursor.rowcount == 0:
                await db.execute(
                    """
                    INSERT INTO dedup_reservations (
                        signature, finding_id, reservation_token, state, issue_number,
                        created_at, lease_expires_at, updated_at
                    ) VALUES (?, ?, '', 'COMMITTED', ?, ?, 1e12, ?);
                    """,
                    (signature, finding_id, issue_number, now, now),
                )
            await db.commit()
            return True

    async def get_reservation(self, signature: str) -> dict | None:
        await self._ensure_init()
        async with await get_db_connection(self.db_path) as db:
            async with db.execute(
                "SELECT * FROM dedup_reservations WHERE signature = ?;", (signature,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None


def check_existing_github_issues(finding: Finding, existing_issues: list[ExistingIssue]) -> bool:
    marker = f"opencontrib:finding:{finding.finding_id}"
    sig = compute_finding_signature(finding)
    sig_marker = f"opencontrib:signature:{sig}"
    for issue in existing_issues:
        body = issue.body_summary or ""
        if marker in body or sig_marker in body:
            return True
    return False


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
        return normalize_path(p)

    def _norm_snip(s: str) -> str:
        return normalize_code(s)

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

    citation_res = validate_verifier_citations(verification, repo_context)
    if not citation_res.valid:
        return GateResult(decision=GateDecision.DENY, reason=f"VERIFIER_CITATION_INVALID: {citation_res.reason}")

    if dedup_result.is_duplicate:
        return GateResult(decision=GateDecision.DENY, reason="DUPLICATE")

    if str(dedup_result.finding_id) != str(finding.finding_id):
        return GateResult(decision=GateDecision.DENY, reason="DEDUP_RESERVATION_MISMATCH")

    expected_signature = compute_finding_signature(finding)
    if dedup_result.signature != expected_signature:
        return GateResult(decision=GateDecision.DENY, reason="SIGNATURE_MISMATCH")

    if verification.duplicate_issue or check_existing_github_issues(finding, repo_context.existing_issues):
        return GateResult(decision=GateDecision.DENY, reason="EXISTING_GITHUB_ISSUE")

    if repo_context.context_completeness == ContextCompleteness.PARTIAL:
        if getattr(finding, "claim_scope", "local") == "repository_wide" or getattr(finding, "depends_on_absence", False):
            return GateResult(decision=GateDecision.DENY, reason="PARTIAL_CONTEXT_GLOBAL_ABSENCE")

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
