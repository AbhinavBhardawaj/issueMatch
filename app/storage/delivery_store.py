import asyncio
from enum import Enum
from typing import Protocol
from pydantic import BaseModel, ConfigDict


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


class InMemoryDeliveryStore:
    """
    Thread-safe, application-scoped delivery store for local delivery idempotency.
    Reused across requests. Not recreated per webhook call.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._deliveries: dict[str, str] = {}  # delivery_id -> body_hash

    async def claim(self, delivery_id: str, body_hash: str) -> DeliveryClaimResult:
        async with self._lock:
            if delivery_id in self._deliveries:
                existing_hash = self._deliveries[delivery_id]
                if existing_hash == body_hash:
                    return DeliveryClaimResult(
                        status=DeliveryClaimStatus.DUPLICATE,
                        delivery_id=delivery_id,
                    )
                else:
                    # Same delivery ID but different payload: security error / fail closed
                    return DeliveryClaimResult(
                        status=DeliveryClaimStatus.MISMATCHED_PAYLOAD,
                        delivery_id=delivery_id,
                    )

            self._deliveries[delivery_id] = body_hash
            return DeliveryClaimResult(
                status=DeliveryClaimStatus.ACCEPTED,
                delivery_id=delivery_id,
            )
