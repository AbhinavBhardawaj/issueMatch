"""Storage-independent candidate state repository."""
from typing import Protocol
from app.models.candidate import CandidateStatus, CandidateSubmission


class CandidateRepository(Protocol):
    async def create(self, candidate: CandidateSubmission) -> CandidateSubmission: ...
    async def get(self, candidate_id: str) -> CandidateSubmission | None: ...
    async def update(self, candidate: CandidateSubmission) -> CandidateSubmission: ...
    async def has_delivery(self, delivery_id: str) -> bool: ...
    async def mark_delivery(self, delivery_id: str) -> None: ...
    async def list_for_issue(self, owner: str, repository: str, issue_number: int) -> list[CandidateSubmission]: ...
    async def update_status(self, candidate_id: str, status: CandidateStatus) -> CandidateSubmission: ...


class InMemoryCandidateRepository:
    """Development/test storage only. It is intentionally not production-persistent."""
    def __init__(self) -> None:
        self._candidates: dict[str, CandidateSubmission] = {}
        self._deliveries: set[str] = set()

    async def create(self, candidate: CandidateSubmission) -> CandidateSubmission:
        if candidate.webhook_delivery_id in self._deliveries:
            raise ValueError("Webhook delivery was already processed")
        self._candidates[candidate.candidate_id] = candidate
        self._deliveries.add(candidate.webhook_delivery_id)
        return candidate
    async def get(self, candidate_id: str) -> CandidateSubmission | None: return self._candidates.get(candidate_id)
    async def update(self, candidate: CandidateSubmission) -> CandidateSubmission:
        if candidate.candidate_id not in self._candidates: raise KeyError(candidate.candidate_id)
        self._candidates[candidate.candidate_id] = candidate
        return candidate
    async def has_delivery(self, delivery_id: str) -> bool: return delivery_id in self._deliveries
    async def mark_delivery(self, delivery_id: str) -> None: self._deliveries.add(delivery_id)
    async def list_for_issue(self, owner: str, repository: str, issue_number: int) -> list[CandidateSubmission]:
        return [c for c in self._candidates.values() if (c.repository_owner, c.repository_name, c.issue_number) == (owner, repository, issue_number)]
    async def update_status(self, candidate_id: str, status: CandidateStatus) -> CandidateSubmission:
        candidate = await self.get(candidate_id)
        if candidate is None: raise KeyError(candidate_id)
        return await self.update(candidate.model_copy(update={"status": status}))
