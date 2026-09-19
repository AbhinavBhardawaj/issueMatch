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


@pytest.mark.asyncio
async def test_delivery_lifecycle_complete_and_failed_retry():
    from app.storage.delivery_store import DeliveryState
    store = InMemoryDeliveryStore()

    # 1. New claim -> PROCESSING
    res1 = await store.claim("del-life", "hash-1")
    assert res1.status == DeliveryClaimStatus.ACCEPTED
    assert await store.get_state("del-life") == DeliveryState.PROCESSING

    # 2. Failure occurs -> FAILED
    ok = await store.fail("del-life", "hash-1")
    assert ok is True
    assert await store.get_state("del-life") == DeliveryState.FAILED

    # 3. Redelivery with same hash -> ACCEPTED (retry permitted, transitions to PROCESSING)
    res2 = await store.claim("del-life", "hash-1")
    assert res2.status == DeliveryClaimStatus.ACCEPTED
    assert await store.get_state("del-life") == DeliveryState.PROCESSING

    # 4. Success occurs -> COMPLETED
    ok = await store.complete("del-life", "hash-1")
    assert ok is True
    assert await store.get_state("del-life") == DeliveryState.COMPLETED

    # 5. Redelivery of completed -> DUPLICATE
    res3 = await store.claim("del-life", "hash-1")
    assert res3.status == DeliveryClaimStatus.DUPLICATE
