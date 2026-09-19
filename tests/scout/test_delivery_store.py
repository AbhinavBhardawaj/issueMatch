import pytest
from app.storage.delivery_store import InMemoryDeliveryStore, DeliveryClaimStatus


@pytest.mark.asyncio
async def test_first_delivery_accepted():
    store = InMemoryDeliveryStore()
    res = await store.claim("del-1", "hash-abc")
    assert res.status == DeliveryClaimStatus.ACCEPTED
    assert res.delivery_id == "del-1"


@pytest.mark.asyncio
async def test_duplicate_delivery_same_hash():
    store = InMemoryDeliveryStore()
    res1 = await store.claim("del-1", "hash-abc")
    res2 = await store.claim("del-1", "hash-abc")
    assert res1.status == DeliveryClaimStatus.ACCEPTED
    assert res2.status == DeliveryClaimStatus.DUPLICATE


@pytest.mark.asyncio
async def test_mismatched_payload_fails_closed():
    store = InMemoryDeliveryStore()
    res1 = await store.claim("del-1", "hash-abc")
    res2 = await store.claim("del-1", "hash-DIFFERENT")
    assert res1.status == DeliveryClaimStatus.ACCEPTED
    assert res2.status == DeliveryClaimStatus.MISMATCHED_PAYLOAD


@pytest.mark.asyncio
async def test_concurrent_claims():
    import asyncio
    store = InMemoryDeliveryStore()

    async def claim():
        return await store.claim("del-concurrent", "hash-123")

    results = await asyncio.gather(claim(), claim(), claim())
    statuses = [r.status for r in results]
    assert statuses.count(DeliveryClaimStatus.ACCEPTED) == 1
    assert statuses.count(DeliveryClaimStatus.DUPLICATE) == 2
