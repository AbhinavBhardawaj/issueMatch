"""Durable in-process webhook worker with lease heartbeats and crash recovery."""
import asyncio
import json
import logging
from typing import Any

from app.github.events import (
    MalformedGitHubEvent,
    normalize_issue_comment_created,
    normalize_push_event,
)
from app.storage.delivery_store import SQLiteDeliveryStore, DeliveryStore

logger = logging.getLogger(__name__)


class DurableWebhookWorker:
    def __init__(
        self,
        delivery_store: DeliveryStore,
        scout_service: Any = None,
        candidate_service: Any = None,
        lease_seconds: float = 60.0,
        poll_interval: float = 1.0,
    ) -> None:
        self.delivery_store = delivery_store
        self.scout_service = scout_service
        self.candidate_service = candidate_service
        self.lease_seconds = lease_seconds
        self.poll_interval = poll_interval
        self._wake_event = asyncio.Event()
        self._running = False
        self._task: asyncio.Task | None = None

    def notify(self) -> None:
        """Wake up worker immediately when new work is enqueued."""
        self._wake_event.set()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("DurableWebhookWorker started")

    async def stop(self) -> None:
        self._running = False
        self.notify()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("DurableWebhookWorker stopped")

    async def process_one(self) -> bool:
        """
        Claims and processes at most one job.
        Returns True if a job was found and processed, False if no job was available.
        """
        if not hasattr(self.delivery_store, "claim_next_available_job"):
            return False

        job = await self.delivery_store.claim_next_available_job(lease_seconds=self.lease_seconds)
        if not job:
            return False

        delivery_id = job["delivery_id"]
        lease_token = job["lease_token"]
        event_type = job["event_type"]
        payload_str = job["payload_json"]

        # Background lease renewal heartbeat
        heartbeat_task = asyncio.create_task(self._heartbeat(delivery_id, lease_token))

        try:
            payload = json.loads(payload_str)
            if event_type == "push" and self.scout_service:
                push_event = normalize_push_event(payload, delivery_id)
                if push_event is None:
                    await self.delivery_store.complete_job(delivery_id, lease_token)
                    return True

                res = await self.scout_service.process_push(push_event)
                if res and getattr(res, "failures", None):
                    err_msg = "; ".join(res.failures)
                    await self.delivery_store.fail_job(
                        delivery_id, lease_token, error_msg=err_msg, retryable=True
                    )
                else:
                    await self.delivery_store.complete_job(delivery_id, lease_token)

            elif event_type == "issue_comment" and self.candidate_service:
                comment_event = normalize_issue_comment_created(payload, delivery_id)
                await self.candidate_service.process_issue_comment(comment_event)
                await self.delivery_store.complete_job(delivery_id, lease_token)

            else:
                await self.delivery_store.complete_job(delivery_id, lease_token)

            return True

        except Exception as exc:
            logger.error(f"Error processing webhook job {delivery_id}: {exc}", exc_info=True)
            try:
                await self.delivery_store.fail_job(
                    delivery_id, lease_token, error_msg=str(exc), retryable=True
                )
            except Exception as fail_err:
                logger.error(f"Failed to record failure for job {delivery_id}: {fail_err}")
            return True

        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

    async def _heartbeat(self, delivery_id: str, lease_token: str) -> None:
        interval = max(self.lease_seconds / 3.0, 1.0)
        while True:
            await asyncio.sleep(interval)
            try:
                if hasattr(self.delivery_store, "renew_job_lease"):
                    renewed = await self.delivery_store.renew_job_lease(
                        delivery_id, lease_token, self.lease_seconds
                    )
                    if not renewed:
                        logger.warning(f"Heartbeat renewal failed for delivery {delivery_id}")
                        break
            except Exception as exc:
                logger.warning(f"Heartbeat renewal exception for delivery {delivery_id}: {exc}")
                break

    async def _run_loop(self) -> None:
        while self._running:
            try:
                processed = await self.process_one()
                if not processed:
                    try:
                        await asyncio.wait_for(
                            self._wake_event.wait(), timeout=self.poll_interval
                        )
                    except asyncio.TimeoutError:
                        pass
                    self._wake_event.clear()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"Worker loop encountered unexpected error: {exc}", exc_info=True)
                await asyncio.sleep(self.poll_interval)
