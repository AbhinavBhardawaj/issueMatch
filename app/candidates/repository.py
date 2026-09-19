"""Storage-independent candidate state repository."""
import asyncio
from typing import Any, Protocol
from app.models.candidate import CandidateStatus, CandidateSubmission, IssueEvaluation


class CandidateRepository(Protocol):
    async def create(self, candidate: CandidateSubmission) -> CandidateSubmission: ...
    async def get(self, candidate_id: str) -> CandidateSubmission | None: ...
    async def update(self, candidate: CandidateSubmission) -> CandidateSubmission: ...
    async def has_delivery(self, delivery_id: str) -> bool: ...
    async def mark_delivery(self, delivery_id: str) -> None: ...
    async def list_for_issue(self, owner: str, repository: str, issue_number: int) -> list[CandidateSubmission]: ...
    async def update_status(self, candidate_id: str, status: CandidateStatus) -> CandidateSubmission: ...
    async def get_issue_state(self, owner: str, repository: str, issue_number: int) -> IssueEvaluation | None: ...
    async def update_issue_state(self, state: IssueEvaluation) -> IssueEvaluation: ...
    async def find_by_comment(self, owner: str, repository: str, issue_number: int, comment_id: int) -> CandidateSubmission | None: ...


class InMemoryCandidateRepository:
    """Development/test storage only. It is intentionally not production-persistent."""
    def __init__(self) -> None:
        self._candidates: dict[str, CandidateSubmission] = {}
        self._deliveries: set[str] = set()
        self._issue_states: dict[tuple[str, str, int], IssueEvaluation] = {}

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

    async def get_issue_state(self, owner: str, repository: str, issue_number: int) -> IssueEvaluation | None:
        return self._issue_states.get((owner, repository, issue_number))

    async def update_issue_state(self, state: IssueEvaluation) -> IssueEvaluation:
        self._issue_states[(state.repository_owner, state.repository_name, state.issue_number)] = state
        return state

    async def find_by_comment(self, owner: str, repository: str, issue_number: int, comment_id: int) -> CandidateSubmission | None:
        return next((candidate for candidate in self._candidates.values() if (
            candidate.repository_owner, candidate.repository_name, candidate.issue_number, candidate.comment_id
        ) == (owner, repository, issue_number, comment_id)), None)


class DynamoDbCandidateRepository:
    """DynamoDB-backed candidate storage for a table keyed by ``pk`` and ``sk``.

    The table must have an ``issue-key-index`` GSI with ``issue_key`` as its
    partition key.  The in-memory implementation remains the local-only option.
    """

    def __init__(self, table_name: str, dynamodb_client: Any | None = None) -> None:
        if not table_name:
            raise ValueError("A DynamoDB table name is required")
        if dynamodb_client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - deployment configuration
                raise RuntimeError("boto3 is required for DynamoDB candidate storage") from exc
            dynamodb_client = boto3.client("dynamodb")
        self._table_name = table_name
        self._client = dynamodb_client

    @staticmethod
    def _candidate_key(candidate_id: str) -> dict[str, dict[str, str]]:
        return {"pk": {"S": f"CANDIDATE#{candidate_id}"}, "sk": {"S": "CANDIDATE"}}

    @staticmethod
    def _delivery_key(delivery_id: str) -> dict[str, dict[str, str]]:
        return {"pk": {"S": f"DELIVERY#{delivery_id}"}, "sk": {"S": "DELIVERY"}}

    @staticmethod
    def _serialize(value: Any) -> dict[str, Any]:
        from boto3.dynamodb.types import TypeSerializer
        return TypeSerializer().serialize(value)

    @staticmethod
    def _deserialize(item: dict[str, Any]) -> dict[str, Any]:
        from boto3.dynamodb.types import TypeDeserializer
        deserializer = TypeDeserializer()
        return {key: deserializer.deserialize(value) for key, value in item.items()}

    @staticmethod
    def _candidate_item(candidate: CandidateSubmission) -> dict[str, dict[str, Any]]:
        data = candidate.model_dump(mode="json")
        data.update({
            "pk": f"CANDIDATE#{candidate.candidate_id}", "sk": "CANDIDATE",
            "issue_key": f"ISSUE#{candidate.repository_owner}#{candidate.repository_name}#{candidate.issue_number}",
        })
        return {key: DynamoDbCandidateRepository._serialize(value) for key, value in data.items()}

    async def create(self, candidate: CandidateSubmission) -> CandidateSubmission:
        """Atomically reserve a delivery ID and persist its associated candidate."""
        delivery_item = self._delivery_key(candidate.webhook_delivery_id)
        delivery_item["candidate_id"] = self._serialize(candidate.candidate_id)
        try:
            await asyncio.to_thread(
                self._client.transact_write_items,
                TransactItems=[
                    {"Put": {"TableName": self._table_name, "Item": delivery_item,
                             "ConditionExpression": "attribute_not_exists(pk)"}},
                    {"Put": {"TableName": self._table_name, "Item": self._candidate_item(candidate),
                             "ConditionExpression": "attribute_not_exists(pk)"}},
                ],
            )
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code in {"TransactionCanceledException", "ConditionalCheckFailedException"}:
                raise ValueError("Webhook delivery was already processed") from exc
            raise
        return candidate

    async def get(self, candidate_id: str) -> CandidateSubmission | None:
        response = await asyncio.to_thread(
            self._client.get_item, TableName=self._table_name, Key=self._candidate_key(candidate_id)
        )
        item = response.get("Item")
        if not item:
            return None
        data = self._deserialize(item)
        return CandidateSubmission.model_validate({key: value for key, value in data.items() if key not in {"pk", "sk", "issue_key"}})

    async def update(self, candidate: CandidateSubmission) -> CandidateSubmission:
        await asyncio.to_thread(
            self._client.put_item, TableName=self._table_name, Item=self._candidate_item(candidate),
            ConditionExpression="attribute_exists(pk)",
        )
        return candidate

    async def has_delivery(self, delivery_id: str) -> bool:
        response = await asyncio.to_thread(
            self._client.get_item, TableName=self._table_name, Key=self._delivery_key(delivery_id),
            ProjectionExpression="pk",
        )
        return "Item" in response

    async def mark_delivery(self, delivery_id: str) -> None:
        try:
            await asyncio.to_thread(
                self._client.put_item, TableName=self._table_name,
                Item=self._delivery_key(delivery_id), ConditionExpression="attribute_not_exists(pk)",
            )
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code not in {"TransactionCanceledException", "ConditionalCheckFailedException"}:
                raise

    async def list_for_issue(self, owner: str, repository: str, issue_number: int) -> list[CandidateSubmission]:
        response = await asyncio.to_thread(
            self._client.query, TableName=self._table_name, IndexName="issue-key-index",
            KeyConditionExpression="issue_key = :issue_key",
            FilterExpression="attribute_exists(candidate_id)",
            ExpressionAttributeValues={":issue_key": self._serialize(f"ISSUE#{owner}#{repository}#{issue_number}")},
        )
        candidates = []
        for item in response.get("Items", []):
            data = self._deserialize(item)
            candidates.append(CandidateSubmission.model_validate({key: value for key, value in data.items() if key not in {"pk", "sk", "issue_key"}}))
        return candidates

    @staticmethod
    def _issue_state_key(state: IssueEvaluation) -> dict[str, dict[str, str]]:
        issue_key = f"{state.repository_owner}#{state.repository_name}#{state.issue_number}"
        return {"pk": {"S": f"STATE#{issue_key}"}, "sk": {"S": "STATE"}}

    async def get_issue_state(self, owner: str, repository: str, issue_number: int) -> IssueEvaluation | None:
        issue_key = f"{owner}#{repository}#{issue_number}"
        response = await asyncio.to_thread(
            self._client.get_item, TableName=self._table_name,
            Key={"pk": {"S": f"STATE#{issue_key}"}, "sk": {"S": "STATE"}},
        )
        item = response.get("Item")
        if not item:
            return None
        data = self._deserialize(item)
        return IssueEvaluation.model_validate({key: value for key, value in data.items() if key not in {"pk", "sk", "issue_key"}})

    async def update_issue_state(self, state: IssueEvaluation) -> IssueEvaluation:
        data = state.model_dump(mode="json")
        data.update({"pk": f"STATE#{state.repository_owner}#{state.repository_name}#{state.issue_number}", "sk": "STATE",
                     "issue_key": f"ISSUE#{state.repository_owner}#{state.repository_name}#{state.issue_number}"})
        await asyncio.to_thread(
            self._client.put_item, TableName=self._table_name,
            Item={key: self._serialize(value) for key, value in data.items()},
        )
        return state

    async def find_by_comment(self, owner: str, repository: str, issue_number: int, comment_id: int) -> CandidateSubmission | None:
        response = await asyncio.to_thread(
            self._client.query, TableName=self._table_name, IndexName="issue-key-index",
            KeyConditionExpression="issue_key = :issue_key", FilterExpression="comment_id = :comment_id",
            ExpressionAttributeValues={":issue_key": self._serialize(f"ISSUE#{owner}#{repository}#{issue_number}"),
                                       ":comment_id": self._serialize(comment_id)},
        )
        item = next(iter(response.get("Items", [])), None)
        if not item:
            return None
        data = self._deserialize(item)
        return CandidateSubmission.model_validate({key: value for key, value in data.items() if key not in {"pk", "sk", "issue_key"}})

    async def update_status(self, candidate_id: str, status: CandidateStatus) -> CandidateSubmission:
        candidate = await self.get(candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        return await self.update(candidate.model_copy(update={"status": status}))
