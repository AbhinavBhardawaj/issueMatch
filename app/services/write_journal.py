"""Durable Issue Write Intent Journal for crash-safe, idempotent GitHub issue publication."""
import secrets
import time
from enum import Enum
from typing import Protocol, Any
from app.domain.models import Finding, Verification, RepoContext
from app.storage.sqlite import get_db_connection, init_schema
from app.services.issue_gate import DedupState


class IntentState(str, Enum):
    PREPARED = "PREPARED"
    POSTING = "POSTING"
    COMMITTED = "COMMITTED"
    ABORTED = "ABORTED"


class UnresolvedWriteIntentError(Exception):
    """Raised when an intent is stuck in POSTING state and reconciliation cannot resolve it."""
    pass


class IssueWriteJournal(Protocol):
    async def get_intent(self, signature: str) -> dict[str, Any] | None: ...
    async def create_write_intent(
        self,
        finding: Finding,
        verification: Verification,
        repo_context: RepoContext,
        signature: str,
        reservation_token: str,
    ) -> str: ...
    async def transition_to_posting(self, signature: str, intent_id: str) -> bool: ...
    async def commit_intent(
        self, signature: str, intent_id: str, issue_number: int, expected_state: str = "POSTING"
    ) -> bool: ...
    async def abort_intent(self, signature: str, intent_id: str) -> bool: ...


class InMemoryIssueWriteJournal:
    def __init__(self) -> None:
        self._intents: dict[str, dict[str, Any]] = {}

    async def get_intent(self, signature: str) -> dict[str, Any] | None:
        return self._intents.get(signature)

    async def create_write_intent(
        self,
        finding: Finding,
        verification: Verification,
        repo_context: RepoContext,
        signature: str,
        reservation_token: str,
    ) -> str:
        existing = self._intents.get(signature)
        if existing and existing["state"] == IntentState.PREPARED.value:
            return existing["intent_id"]

        intent_id = secrets.token_hex(16)
        attempt_id = secrets.token_hex(8)
        now = time.time()
        marker = f"<!-- opencontrib:finding:{finding.finding_id}:v:{verification.verification_id} -->"

        self._intents[signature] = {
            "signature": signature,
            "intent_id": intent_id,
            "finding_id": str(finding.finding_id),
            "verification_id": str(verification.verification_id),
            "installation_id": str(finding.installation_id),
            "repository_id": str(finding.repository_id),
            "commit_sha": str(finding.commit_sha),
            "owner": repo_context.owner,
            "repo_name": repo_context.name,
            "marker": marker,
            "state": IntentState.PREPARED.value,
            "issue_number": None,
            "attempt_id": attempt_id,
            "created_at": now,
            "updated_at": now,
        }
        return intent_id

    async def transition_to_posting(self, signature: str, intent_id: str) -> bool:
        record = self._intents.get(signature)
        if record and record["intent_id"] == intent_id and record["state"] == IntentState.PREPARED.value:
            record["state"] = IntentState.POSTING.value
            record["updated_at"] = time.time()
            return True
        return False

    async def commit_intent(
        self, signature: str, intent_id: str, issue_number: int, expected_state: str = "POSTING"
    ) -> bool:
        record = self._intents.get(signature)
        if record and record["intent_id"] == intent_id and record["state"] == expected_state:
            record["state"] = IntentState.COMMITTED.value
            record["issue_number"] = issue_number
            record["updated_at"] = time.time()
            return True
        return False

    async def abort_intent(self, signature: str, intent_id: str) -> bool:
        record = self._intents.get(signature)
        if record and record["intent_id"] == intent_id and record["state"] == IntentState.PREPARED.value:
            record["state"] = IntentState.ABORTED.value
            record["updated_at"] = time.time()
            return True
        return False


class SQLiteIssueWriteJournal:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._initialized = False

    async def _ensure_init(self) -> None:
        if not self._initialized:
            await init_schema(self.db_path)
            self._initialized = True

    async def get_intent(self, signature: str) -> dict[str, Any] | None:
        await self._ensure_init()
        async with await get_db_connection(self.db_path) as db:
            async with db.execute(
                "SELECT * FROM issue_write_intents WHERE signature = ?;", (signature,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def create_write_intent(
        self,
        finding: Finding,
        verification: Verification,
        repo_context: RepoContext,
        signature: str,
        reservation_token: str,
    ) -> str:
        await self._ensure_init()
        now = time.time()
        marker = f"<!-- opencontrib:finding:{finding.finding_id}:v:{verification.verification_id} -->"

        async with await get_db_connection(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE;")

            # 1. Verify dedup reservation is valid and transition to WRITE_PENDING
            async with db.execute(
                "SELECT finding_id, reservation_token, state, lease_expires_at FROM dedup_reservations WHERE signature = ?;",
                (signature,),
            ) as cursor:
                dedup_row = await cursor.fetchone()

            if not dedup_row:
                await db.rollback()
                raise ValueError(f"No reservation found for signature {signature}")

            if str(dedup_row["finding_id"]) != str(finding.finding_id):
                await db.rollback()
                raise ValueError("Reservation finding_id mismatch")

            if not secrets.compare_digest(dedup_row["reservation_token"] or "", reservation_token or ""):
                await db.rollback()
                raise ValueError("Reservation token mismatch")

            if dedup_row["state"] == DedupState.RESERVED.value:
                if dedup_row["lease_expires_at"] <= now:
                    await db.rollback()
                    raise ValueError("Reservation has expired")
                # Atomically transition to WRITE_PENDING
                cursor = await db.execute(
                    """
                    UPDATE dedup_reservations
                    SET state = 'WRITE_PENDING', updated_at = ?
                    WHERE signature = ? AND finding_id = ? AND reservation_token = ? AND state = 'RESERVED' AND lease_expires_at > ?;
                    """,
                    (now, signature, str(finding.finding_id), reservation_token, now),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    raise ValueError("Failed to transition dedup reservation to WRITE_PENDING")
            elif dedup_row["state"] != DedupState.WRITE_PENDING.value:
                await db.rollback()
                raise ValueError(f"Invalid dedup state for write intent: {dedup_row['state']}")

            # 2. Check existing write intent
            async with db.execute(
                "SELECT intent_id, state FROM issue_write_intents WHERE signature = ?;",
                (signature,),
            ) as cursor:
                intent_row = await cursor.fetchone()

            if intent_row:
                intent_id = intent_row["intent_id"]
                state = intent_row["state"]
                if state == IntentState.PREPARED.value:
                    await db.commit()
                    return intent_id
                elif state == IntentState.POSTING.value:
                    # In ambiguous POSTING state: return intent_id for reconciliation
                    await db.commit()
                    return intent_id
                elif state == IntentState.COMMITTED.value:
                    await db.commit()
                    return intent_id
                else: # ABORTED -> allow creating new attempt
                    new_intent_id = secrets.token_hex(16)
                    attempt_id = secrets.token_hex(8)
                    await db.execute(
                        """
                        UPDATE issue_write_intents
                        SET intent_id = ?, state = 'PREPARED', attempt_id = ?, updated_at = ?
                        WHERE signature = ?;
                        """,
                        (new_intent_id, attempt_id, now, signature),
                    )
                    await db.commit()
                    return new_intent_id

            # New intent
            intent_id = secrets.token_hex(16)
            attempt_id = secrets.token_hex(8)
            await db.execute(
                """
                INSERT INTO issue_write_intents (
                    signature, intent_id, finding_id, verification_id,
                    installation_id, repository_id, commit_sha, owner, repo_name,
                    marker, state, issue_number, attempt_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PREPARED', NULL, ?, ?, ?);
                """,
                (
                    signature,
                    intent_id,
                    str(finding.finding_id),
                    str(verification.verification_id),
                    str(finding.installation_id),
                    str(finding.repository_id),
                    str(finding.commit_sha),
                    repo_context.owner,
                    repo_context.name,
                    marker,
                    attempt_id,
                    now,
                    now,
                ),
            )
            await db.commit()
            return intent_id

    async def transition_to_posting(self, signature: str, intent_id: str) -> bool:
        await self._ensure_init()
        now = time.time()
        async with await get_db_connection(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE;")
            cursor = await db.execute(
                """
                UPDATE issue_write_intents
                SET state = 'POSTING', updated_at = ?
                WHERE signature = ? AND intent_id = ? AND state = 'PREPARED';
                """,
                (now, signature, intent_id),
            )
            updated = cursor.rowcount == 1
            if updated:
                await db.commit()
            else:
                await db.rollback()
            return updated

    async def commit_intent(
        self, signature: str, intent_id: str, issue_number: int, expected_state: str = "POSTING"
    ) -> bool:
        await self._ensure_init()
        now = time.time()
        async with await get_db_connection(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE;")
            cursor = await db.execute(
                """
                UPDATE issue_write_intents
                SET state = 'COMMITTED', issue_number = ?, updated_at = ?
                WHERE signature = ? AND intent_id = ? AND state = ?;
                """,
                (issue_number, now, signature, intent_id, expected_state),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return False

            dedup_cursor = await db.execute(
                """
                UPDATE dedup_reservations
                SET state = 'COMMITTED', issue_number = ?, updated_at = ?
                WHERE signature = ? AND state = 'WRITE_PENDING';
                """,
                (issue_number, now, signature),
            )
            if dedup_cursor.rowcount != 1:
                async with db.execute(
                    "SELECT state, issue_number FROM dedup_reservations WHERE signature = ?;", (signature,)
                ) as d_cur:
                    d_row = await d_cur.fetchone()
                    if not d_row or d_row["state"] != DedupState.COMMITTED.value or d_row["issue_number"] != issue_number:
                        await db.rollback()
                        return False

            await db.commit()
            return True

    async def abort_intent(self, signature: str, intent_id: str) -> bool:
        await self._ensure_init()
        now = time.time()
        async with await get_db_connection(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE;")
            cursor = await db.execute(
                """
                UPDATE issue_write_intents
                SET state = 'ABORTED', updated_at = ?
                WHERE signature = ? AND intent_id = ? AND state = 'PREPARED';
                """,
                (now, signature, intent_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return False

            # Only revert dedup if it is currently WRITE_PENDING. Never revert COMMITTED!
            await db.execute(
                """
                UPDATE dedup_reservations
                SET state = 'RESERVED', updated_at = ?
                WHERE signature = ? AND state = 'WRITE_PENDING';
                """,
                (now, signature),
            )
            await db.commit()
            return True
