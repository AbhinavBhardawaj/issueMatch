"""Storage-independent candidate state repository."""
import asyncio
from decimal import Decimal
import math
import os
from typing import Any, Protocol
from app.models.candidate import CandidateStatus, CandidateSubmission, IssueEvaluation


class CandidateRepository(Protocol):
    async def create(self, candidate: CandidateSubmission) -> CandidateSubmission: ...
    async def get(self, candidate_id: str) -> CandidateSubmission | None: ...
    async def update(self, candidate: CandidateSubmission) -> CandidateSubmission: ...
    async def has_delivery(self, delivery_id: str) -> bool: ...
    async def get_for_delivery(self, delivery_id: str) -> CandidateSubmission | None: ...
    async def mark_delivery(self, delivery_id: str) -> None: ...
    async def list_for_issue(self, owner: str, repository: str, issue_number: int) -> list[CandidateSubmission]: ...
    async def update_status(self, candidate_id: str, status: CandidateStatus) -> CandidateSubmission: ...
    async def get_issue_state(self, owner: str, repository: str, issue_number: int) -> IssueEvaluation | None: ...
    async def update_issue_state(self, state: IssueEvaluation) -> IssueEvaluation: ...
    async def find_by_comment(self, owner: str, repository: str, issue_number: int, comment_id: int) -> CandidateSubmission | None: ...


class DynamoDbConfigurationError(RuntimeError):
    """Invalid local configuration for the DynamoDB candidate store."""


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
    async def get_for_delivery(self, delivery_id: str) -> CandidateSubmission | None:
        return next((candidate for candidate in self._candidates.values()
                     if candidate.webhook_delivery_id == delivery_id), None)
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

    The table must have an ``issue-key-index`` GSI with either ``issue_key``
    or ``issue-key`` as its string partition key. The
    in-memory implementation remains the local-only option.
    """

    def __init__(self, table_name: str, dynamodb_client: Any | None = None) -> None:
        if not table_name:
            raise ValueError("A DynamoDB table name is required")
        if dynamodb_client is None:
            try:
                import boto3
                from botocore.exceptions import NoRegionError, ProfileNotFound
            except ImportError as exc:  # pragma: no cover - deployment configuration
                raise RuntimeError("boto3 is required for DynamoDB candidate storage") from exc
            try:
                # Creating a client does not prove credentials are available: Boto3
                # may wait until the first request to raise NoCredentialsError.
                # Fail at startup instead of accepting webhooks we cannot process.
                session = boto3.Session()
                if session.get_credentials() is None:
                    raise DynamoDbConfigurationError(
                        "AWS credentials are not configured for DynamoDB. Configure "
                        "AWS_PROFILE (including SSO login), an IAM role, or "
                        "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY "
                        "(plus AWS_SESSION_TOKEN for temporary credentials)."
                    )
                # Boto3 discovers AWS_DEFAULT_REGION and profile configuration
                # itself, but does not read the project's documented AWS_REGION.
                dynamodb_client = session.client(
                    "dynamodb", region_name=os.getenv("AWS_REGION") or None
                )
            except NoRegionError as exc:
                raise DynamoDbConfigurationError(
                    "DynamoDB region is not configured. Set AWS_REGION or "
                    "AWS_DEFAULT_REGION to the region containing DYNAMODB_TABLE."
                ) from exc
            except ProfileNotFound as exc:
                raise DynamoDbConfigurationError(
                    "AWS_PROFILE does not refer to a configured AWS profile. "
                    "Configure the profile or select a valid one."
                ) from exc
        self._table_name = table_name
        self._client = dynamodb_client
        self._issue_key_attribute = "issue_key"

    def check_read_access(self) -> None:
        """Fail startup early if the table or its index cannot be queried.

        This reads only a reserved, nonexistent key and never writes data.
        Other required write/query permissions are documented separately.
        """
        try:
            self._client.get_item(
                TableName=self._table_name,
                Key=self._delivery_key("__issuematch_startup_check__"),
                ProjectionExpression="pk",
            )
            self._detect_issue_key_attribute()
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code in {"AccessDeniedException", "AccessDenied"}:
                raise DynamoDbConfigurationError(
                    "AWS identity cannot perform dynamodb:GetItem on DYNAMODB_TABLE or dynamodb:Query "
                    "on issue-key-index. "
                    "Grant the backend identity GetItem and PutItem on the "
                    "candidate table, plus Query on its issue-key-index."
                ) from exc
            if code == "ResourceNotFoundException":
                raise DynamoDbConfigurationError(
                    "DYNAMODB_TABLE was not found in the configured AWS region/account."
                ) from exc
            raise

    def _detect_issue_key_attribute(self) -> None:
        """Probe the GSI without reading user data; support both deployed key spellings."""
        for key_name in ("issue_key", "issue-key"):
            try:
                self._client.query(
                    TableName=self._table_name,
                    IndexName="issue-key-index",
                    KeyConditionExpression="#issue_key = :issue_key",
                    ExpressionAttributeNames={"#issue_key": key_name},
                    ExpressionAttributeValues={":issue_key": {"S": "ISSUE#__issuematch_startup_check__"}},
                    Select="COUNT",
                    Limit=1,
                )
                self._issue_key_attribute = key_name
                return
            except Exception as exc:
                error = getattr(exc, "response", {}).get("Error", {})
                if (key_name == "issue_key" and error.get("Code") == "ValidationException"
                        and "Query condition missed key schema element" in error.get("Message", "")):
                    continue
                if error.get("Code") == "ValidationException":
                    raise DynamoDbConfigurationError(
                        "issue-key-index has an unsupported key schema. Its string "
                        "partition key must be issue_key or issue-key."
                    ) from exc
                raise

    @staticmethod
    def _candidate_key(candidate_id: str) -> dict[str, dict[str, str]]:
        return {"pk": {"S": f"CANDIDATE#{candidate_id}"}, "sk": {"S": "CANDIDATE"}}

    @staticmethod
    def _delivery_key(delivery_id: str) -> dict[str, dict[str, str]]:
        return {"pk": {"S": f"DELIVERY#{delivery_id}"}, "sk": {"S": "DELIVERY"}}

    @staticmethod
    def _serialize(value: Any) -> dict[str, Any]:
        from boto3.dynamodb.types import TypeSerializer
        # Pydantic's JSON-mode dump contains Python floats (notably analysis
        # confidence), while boto3 deliberately refuses floats at *any* depth.
        # Convert the complete JSON value before the recursive boto3 serializer
        # sees it; converting only top-level fields misses analysis_result.
        def dynamodb_numbers(item: Any) -> Any:
            if isinstance(item, float):
                if not math.isfinite(item):
                    raise ValueError("Non-finite numbers cannot be stored in DynamoDB")
                return Decimal(str(item))
            if isinstance(item, dict):
                return {key: dynamodb_numbers(nested) for key, nested in item.items()}
            if isinstance(item, list):
                return [dynamodb_numbers(nested) for nested in item]
            return item

        return TypeSerializer().serialize(dynamodb_numbers(value))

    @staticmethod
    def _deserialize(item: dict[str, Any]) -> dict[str, Any]:
        from boto3.dynamodb.types import TypeDeserializer
        deserializer = TypeDeserializer()
        # Convert boto3 Decimal values back to JSON numbers, including nested
        # analysis_result fields. Otherwise a read/update would turn confidence
        # into a JSON string when Pydantic serializes the candidate again.
        def json_numbers(value: Any) -> Any:
            if isinstance(value, Decimal):
                return int(value) if value == value.to_integral_value() else float(value)
            if isinstance(value, dict):
                return {key: json_numbers(nested) for key, nested in value.items()}
            if isinstance(value, list):
                return [json_numbers(nested) for nested in value]
            return value

        return {key: json_numbers(deserializer.deserialize(value)) for key, value in item.items()}

    @staticmethod
    def _candidate_item(candidate: CandidateSubmission) -> dict[str, dict[str, Any]]:
        data = candidate.model_dump(mode="json")
        data.update({
            "pk": f"CANDIDATE#{candidate.candidate_id}", "sk": "CANDIDATE",
            "issue_key": f"ISSUE#{candidate.repository_owner}#{candidate.repository_name}#{candidate.issue_number}",
        })
        data["issue-key"] = data["issue_key"]
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
            self._client.get_item, TableName=self._table_name,
            Key=self._candidate_key(candidate_id), ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            return None
        data = self._deserialize(item)
        return CandidateSubmission.model_validate({key: value for key, value in data.items() if key not in {"pk", "sk", "issue_key", "issue-key"}})

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

    async def get_for_delivery(self, delivery_id: str) -> CandidateSubmission | None:
        """Read the delivery marker, then the candidate by its primary key.

        This avoids an eventually consistent GSI lookup while recovering a
        partially processed webhook after a worker retry.
        """
        response = await asyncio.to_thread(
            self._client.get_item, TableName=self._table_name,
            Key=self._delivery_key(delivery_id), ConsistentRead=True,
        )
        candidate_id = response.get("Item", {}).get("candidate_id", {}).get("S")
        return await self.get(candidate_id) if candidate_id else None

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
        candidates = []
        query_args = {
            "TableName": self._table_name,
            "IndexName": "issue-key-index",
            "KeyConditionExpression": "#issue_key = :issue_key",
            "ExpressionAttributeNames": {"#issue_key": self._issue_key_attribute},
            "ExpressionAttributeValues": {":issue_key": self._serialize(f"ISSUE#{owner}#{repository}#{issue_number}")},
        }
        while True:
            response = await asyncio.to_thread(self._client.query, **query_args)
            for indexed_item in response.get("Items", []):
                if not indexed_item.get("pk", {}).get("S", "").startswith("CANDIDATE#"):
                    continue
                item_response = await asyncio.to_thread(
                    self._client.get_item, TableName=self._table_name,
                    Key={"pk": indexed_item["pk"], "sk": indexed_item["sk"]},
                )
                item = item_response.get("Item")
                if item:
                    data = self._deserialize(item)
                    candidates.append(CandidateSubmission.model_validate({
                        key: value for key, value in data.items()
                        if key not in {"pk", "sk", "issue_key", "issue-key"}
                    }))
            if not response.get("LastEvaluatedKey"):
                break
            query_args["ExclusiveStartKey"] = response["LastEvaluatedKey"]
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
        return IssueEvaluation.model_validate({key: value for key, value in data.items() if key not in {"pk", "sk", "issue_key", "issue-key"}})

    async def update_issue_state(self, state: IssueEvaluation) -> IssueEvaluation:
        data = state.model_dump(mode="json")
        data.update({"pk": f"STATE#{state.repository_owner}#{state.repository_name}#{state.issue_number}", "sk": "STATE",
                     "issue_key": f"ISSUE#{state.repository_owner}#{state.repository_name}#{state.issue_number}"})
        data["issue-key"] = data["issue_key"]
        await asyncio.to_thread(
            self._client.put_item, TableName=self._table_name,
            Item={key: self._serialize(value) for key, value in data.items()},
        )
        return state

    async def find_by_comment(self, owner: str, repository: str, issue_number: int, comment_id: int) -> CandidateSubmission | None:
        return next(
            (candidate for candidate in await self.list_for_issue(owner, repository, issue_number)
             if candidate.comment_id == comment_id),
            None,
        )

    async def update_status(self, candidate_id: str, status: CandidateStatus) -> CandidateSubmission:
        candidate = await self.get(candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        return await self.update(candidate.model_copy(update={"status": status}))
