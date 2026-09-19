import asyncio
from enum import Enum
from typing import Protocol
from pydantic import BaseModel, ConfigDict


class DeliveryState(str, Enum):
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class DeliveryClaimStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    MISMATCHED_PAYLOAD = "MISMATCHED_PAYLOAD"


class DeliveryClaimResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: DeliveryClaimStatus
    delivery_id: str


class DeliveryStore(Protocol):
    async def claim(self, delivery_id: str, body_hash: str) -> DeliveryClaimResult: ...
    async def complete(self, delivery_id: str, body_hash: str) -> bool: ...
    async def fail(self, delivery_id: str, body_hash: str) -> bool: ...
    async def get_state(self, delivery_id: str) -> DeliveryState | None: ...


class InMemoryDeliveryStore:
    """
    Thread-safe, application-scoped delivery store with full lifecycle idempotency:
    PROCESSING -> COMPLETED / FAILED.
    Allows retrying failed deliveries while preventing duplicate or concurrent execution.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._deliveries: dict[str, dict] = {}  # delivery_id -> {"body_hash": str, "state": DeliveryState}

    async def claim(self, delivery_id: str, body_hash: str) -> DeliveryClaimResult:
        async with self._lock:
            if delivery_id in self._deliveries:
                existing = self._deliveries[delivery_id]
                if existing["body_hash"] != body_hash:
                    # Same delivery ID but different payload: fail closed
                    return DeliveryClaimResult(
                        status=DeliveryClaimStatus.MISMATCHED_PAYLOAD,
                        delivery_id=delivery_id,
                    )
                if existing["state"] == DeliveryState.FAILED:
                    # Redelivery of failed delivery: transition back to PROCESSING
                    existing["state"] = DeliveryState.PROCESSING
                    return DeliveryClaimResult(
                        status=DeliveryClaimStatus.ACCEPTED,
                        delivery_id=delivery_id,
                    )
                # COMPLETED or active PROCESSING: duplicate delivery
                return DeliveryClaimResult(
                    status=DeliveryClaimStatus.DUPLICATE,
                    delivery_id=delivery_id,
                )

            self._deliveries[delivery_id] = {
                "body_hash": body_hash,
                "state": DeliveryState.PROCESSING,
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
