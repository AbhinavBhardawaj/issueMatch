import asyncio
import json
import secrets
import time
from enum import Enum
from typing import Protocol, Any
from pydantic import BaseModel, ConfigDict
import aiosqlite
from app.storage.sqlite import get_db_connection, init_schema


class DeliveryState(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class DeliveryClaimStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    MISMATCHED_PAYLOAD = "MISMATCHED_PAYLOAD"
    EXHAUSTED = "EXHAUSTED"


class DeliveryClaimResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: DeliveryClaimStatus
    delivery_id: str


class DeliveryStore(Protocol):
    async def claim(self, delivery_id: str, body_hash: str) -> DeliveryClaimResult: ...
    async def claim_and_enqueue(
        self,
        delivery_id: str,
        body_hash: str,
        event_type: str,
        payload_json: str,
        max_attempts: int = 3,
    ) -> DeliveryClaimResult: ...
    async def complete(self, delivery_id: str, body_hash: str) -> bool: ...
    async def fail(self, delivery_id: str, body_hash: str) -> bool: ...
    async def get_state(self, delivery_id: str) -> DeliveryState | None: ...


class InMemoryDeliveryStore:
    """
    Thread-safe, application-scoped in-memory delivery store for testing.
    """

    is_durable: bool = False

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._deliveries: dict[str, dict] = {}

    async def claim(self, delivery_id: str, body_hash: str) -> DeliveryClaimResult:
        return await self.claim_and_enqueue(
            delivery_id=delivery_id,
            body_hash=body_hash,
            event_type="push",
            payload_json="{}",
        )

    async def claim_and_enqueue(
        self,
        delivery_id: str,
        body_hash: str,
        event_type: str,
        payload_json: str,
        max_attempts: int = 3,
    ) -> DeliveryClaimResult:
        async with self._lock:
            now = time.time()
            if delivery_id in self._deliveries:
                existing = self._deliveries[delivery_id]
                if existing["body_hash"] != body_hash:
                    return DeliveryClaimResult(
                        status=DeliveryClaimStatus.MISMATCHED_PAYLOAD,
                        delivery_id=delivery_id,
                    )
                if existing["state"] == DeliveryState.FAILED:
                    if existing.get("attempt_count", 1) >= existing.get("max_attempts", 3):
                        return DeliveryClaimResult(
                            status=DeliveryClaimStatus.EXHAUSTED,
                            delivery_id=delivery_id,
                        )
                    existing["state"] = DeliveryState.PROCESSING
                    existing["updated_at"] = now
                    return DeliveryClaimResult(
                        status=DeliveryClaimStatus.ACCEPTED,
                        delivery_id=delivery_id,
                    )
                return DeliveryClaimResult(
                    status=DeliveryClaimStatus.DUPLICATE,
                    delivery_id=delivery_id,
                )

            self._deliveries[delivery_id] = {
                "delivery_id": delivery_id,
                "body_hash": body_hash,
                "event_type": event_type,
                "payload_json": payload_json,
                "state": DeliveryState.PROCESSING,
                "attempt_count": 1,
                "max_attempts": max_attempts,
                "available_at": now,
                "lease_token": secrets.token_hex(16),
                "lease_expires_at": now + 60.0,
                "created_at": now,
                "updated_at": now,
                "last_error": None,
            }
            return DeliveryClaimResult(
                status=DeliveryClaimStatus.ACCEPTED,
                delivery_id=delivery_id,
            )

    async def complete(self, delivery_id: str, body_hash: str) -> bool:
        async with self._lock:
            if delivery_id in self._deliveries:
                if self._deliveries[delivery_id]["body_hash"] == body_hash:
                    self._deliveries[delivery_id]["state"] = DeliveryState.COMPLETED
                    return True
            return False

    async def fail(self, delivery_id: str, body_hash: str) -> bool:
        async with self._lock:
            if delivery_id in self._deliveries:
                if self._deliveries[delivery_id]["body_hash"] == body_hash:
                    self._deliveries[delivery_id]["state"] = DeliveryState.FAILED
                    return True
            return False

    async def get_state(self, delivery_id: str) -> DeliveryState | None:
        async with self._lock:
            record = self._deliveries.get(delivery_id)
            return record["state"] if record else None


class SQLiteDeliveryStore:
    """
    Durable, crash-resilient SQLite-backed webhook delivery and job store.
    Enforces multi-worker leases, body-hash verification, and atomic state transitions.
    """

    is_durable: bool = True

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._initialized = False

    async def _ensure_init(self) -> None:
        if not self._initialized:
            await init_schema(self.db_path)
            self._initialized = True

    async def claim(self, delivery_id: str, body_hash: str) -> DeliveryClaimResult:
        return await self.claim_and_enqueue(
            delivery_id=delivery_id,
            body_hash=body_hash,
            event_type="push",
            payload_json="{}",
        )

    async def claim_and_enqueue(
        self,
        delivery_id: str,
        body_hash: str,
        event_type: str,
        payload_json: str,
        max_attempts: int = 3,
    ) -> DeliveryClaimResult:
        await self._ensure_init()
        now = time.time()
        async with await get_db_connection(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE;")
            async with db.execute(
                "SELECT body_hash, state, attempt_count, max_attempts FROM webhook_jobs WHERE delivery_id = ?;",
                (delivery_id,),
            ) as cursor:
                row = await cursor.fetchone()

            if row:
                existing_hash, state, attempt_count, row_max_attempts = (
                    row["body_hash"],
                    row["state"],
                    row["attempt_count"],
                    row["max_attempts"],
                )
                if existing_hash != body_hash:
                    await db.rollback()
                    return DeliveryClaimResult(
                        status=DeliveryClaimStatus.MISMATCHED_PAYLOAD,
                        delivery_id=delivery_id,
                    )

                if state == DeliveryState.FAILED:
                    if attempt_count >= row_max_attempts:
                        await db.rollback()
                        return DeliveryClaimResult(
                            status=DeliveryClaimStatus.EXHAUSTED,
                            delivery_id=delivery_id,
                        )
                    # Safe retry allowed
                    await db.execute(
                        """
                        UPDATE webhook_jobs
                        SET state = 'PENDING', available_at = ?, updated_at = ?
                        WHERE delivery_id = ?;
                        """,
                        (now, now, delivery_id),
                    )
                    await db.commit()
                    return DeliveryClaimResult(
                        status=DeliveryClaimStatus.ACCEPTED,
                        delivery_id=delivery_id,
                    )

                await db.rollback()
                return DeliveryClaimResult(
                    status=DeliveryClaimStatus.DUPLICATE,
                    delivery_id=delivery_id,
                )

            await db.execute(
                """
                INSERT INTO webhook_jobs (
                    delivery_id, body_hash, event_type, payload_json, state,
                    attempt_count, max_attempts, available_at, lease_token,
                    lease_expires_at, created_at, updated_at, last_error
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, NULL, NULL, ?, ?, NULL);
                """,
                (
                    delivery_id,
                    body_hash,
                    event_type,
                    payload_json,
                    DeliveryState.PENDING.value,
                    max_attempts,
                    now,
                    now,
                    now,
                ),
            )
            await db.commit()
            return DeliveryClaimResult(
                status=DeliveryClaimStatus.ACCEPTED,
                delivery_id=delivery_id,
            )

    async def claim_next_available_job(
        self,
        lease_seconds: float = 60.0,
    ) -> dict[str, Any] | None:
        await self._ensure_init()
        now = time.time()
        lease_token = secrets.token_hex(16)
        lease_expires_at = now + lease_seconds

        async with await get_db_connection(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE;")

            # 1. Terminalize expired jobs that have already reached or exceeded max_attempts
            await db.execute(
                """
                UPDATE webhook_jobs
                SET state = 'FAILED', last_error = 'Lease expired after exhausting max attempts', updated_at = ?
                WHERE state = 'PROCESSING' AND lease_expires_at <= ? AND attempt_count >= max_attempts;
                """,
                (now, now),
            )

            # 2. Select 1 available job
            async with db.execute(
                """
                SELECT delivery_id, body_hash, event_type, payload_json, attempt_count, max_attempts
                FROM webhook_jobs
                WHERE (state = 'PENDING' AND available_at <= ?)
                   OR (state = 'PROCESSING' AND lease_expires_at <= ? AND attempt_count < max_attempts)
                ORDER BY created_at ASC
                LIMIT 1;
                """,
                (now, now),
            ) as cursor:
                row = await cursor.fetchone()

            if not row:
                await db.commit()
                return None

            delivery_id = row["delivery_id"]
            new_attempt_count = row["attempt_count"] + 1

            await db.execute(
                """
                UPDATE webhook_jobs
                SET state = 'PROCESSING',
                    attempt_count = ?,
                    lease_token = ?,
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE delivery_id = ?;
                """,
                (new_attempt_count, lease_token, lease_expires_at, now, delivery_id),
            )
            await db.commit()

            return {
                "delivery_id": delivery_id,
                "body_hash": row["body_hash"],
                "event_type": row["event_type"],
                "payload_json": row["payload_json"],
                "state": DeliveryState.PROCESSING.value,
                "attempt_count": new_attempt_count,
                "max_attempts": row["max_attempts"],
                "lease_token": lease_token,
                "lease_expires_at": lease_expires_at,
            }

    async def renew_job_lease(
        self,
        delivery_id: str,
        lease_token: str,
        lease_seconds: float = 60.0,
    ) -> bool:
        await self._ensure_init()
        now = time.time()
        new_expires_at = now + lease_seconds

        async with await get_db_connection(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE;")
            cursor = await db.execute(
                """
                UPDATE webhook_jobs
                SET lease_expires_at = ?, updated_at = ?
                WHERE delivery_id = ?
                  AND lease_token = ?
                  AND state = 'PROCESSING'
                  AND lease_expires_at > ?;
                """,
                (new_expires_at, now, delivery_id, lease_token, now),
            )
            updated = cursor.rowcount > 0
            if updated:
                await db.commit()
            else:
                await db.rollback()
            return updated

    async def complete_job(self, delivery_id: str, lease_token: str) -> bool:
        await self._ensure_init()
        now = time.time()
        async with await get_db_connection(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE;")
            cursor = await db.execute(
                """
                UPDATE webhook_jobs
                SET state = 'COMPLETED', lease_token = NULL, updated_at = ?
                WHERE delivery_id = ?
                  AND lease_token = ?
                  AND state = 'PROCESSING'
                  AND lease_expires_at > ?;
                """,
                (now, delivery_id, lease_token, now),
            )
            updated = cursor.rowcount > 0
            if updated:
                await db.commit()
            else:
                await db.rollback()
            return updated

    async def fail_job(
        self,
        delivery_id: str,
        lease_token: str,
        error_msg: str,
        retryable: bool = True,
        backoff_seconds: float = 5.0,
    ) -> bool:
        await self._ensure_init()
        now = time.time()
        async with await get_db_connection(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE;")
            async with db.execute(
                "SELECT attempt_count, max_attempts FROM webhook_jobs WHERE delivery_id = ? AND lease_token = ? AND state = 'PROCESSING';",
                (delivery_id, lease_token),
            ) as cursor:
                row = await cursor.fetchone()

            if not row:
                await db.rollback()
                return False

            attempt_count, max_attempts = row["attempt_count"], row["max_attempts"]
            if retryable and attempt_count < max_attempts:
                new_state = DeliveryState.PENDING.value
                available_at = now + backoff_seconds
            else:
                new_state = DeliveryState.FAILED.value
                available_at = now

            await db.execute(
                """
                UPDATE webhook_jobs
                SET state = ?,
                    available_at = ?,
                    lease_token = NULL,
                    last_error = ?,
                    updated_at = ?
                WHERE delivery_id = ? AND lease_token = ?;
                """,
                (new_state, available_at, error_msg, now, delivery_id, lease_token),
            )
            await db.commit()
            return True

    # Legacy compatibility methods
    async def complete(self, delivery_id: str, body_hash: str) -> bool:
        await self._ensure_init()
        now = time.time()
        async with await get_db_connection(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE;")
            cursor = await db.execute(
                """
                UPDATE webhook_jobs
                SET state = 'COMPLETED', lease_token = NULL, updated_at = ?
                WHERE delivery_id = ? AND body_hash = ?;
                """,
                (now, delivery_id, body_hash),
            )
            updated = cursor.rowcount > 0
            if updated:
                await db.commit()
            else:
                await db.rollback()
            return updated

    async def fail(self, delivery_id: str, body_hash: str) -> bool:
        await self._ensure_init()
        now = time.time()
        async with await get_db_connection(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE;")
            cursor = await db.execute(
                """
                UPDATE webhook_jobs
                SET state = 'FAILED', lease_token = NULL, updated_at = ?
                WHERE delivery_id = ? AND body_hash = ?;
                """,
                (now, delivery_id, body_hash),
            )
            updated = cursor.rowcount > 0
            if updated:
                await db.commit()
            else:
                await db.rollback()
            return updated

    async def get_state(self, delivery_id: str) -> DeliveryState | None:
        await self._ensure_init()
        async with await get_db_connection(self.db_path) as db:
            async with db.execute(
                "SELECT state FROM webhook_jobs WHERE delivery_id = ?;", (delivery_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return DeliveryState(row["state"]) if row else None

    async def get_job(self, delivery_id: str) -> dict[str, Any] | None:
        await self._ensure_init()
        async with await get_db_connection(self.db_path) as db:
            async with db.execute(
                "SELECT * FROM webhook_jobs WHERE delivery_id = ?;", (delivery_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None
