import os
import math
import unittest
from unittest.mock import patch

from botocore.exceptions import ClientError, NoRegionError, ProfileNotFound

from app.candidates.repository import (
    DynamoDbCandidateRepository,
    DynamoDbConfigurationError,
    InMemoryCandidateRepository,
)
from app.models.candidate import CandidateStatus, CandidateSubmission
def candidate(delivery="d"):
    return CandidateSubmission(repository_owner="o",repository_name="r",issue_number=1,contributor_username="u",comment_id=1,comment_body="x",approach="x",webhook_delivery_id=delivery)
class RepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_marker_recovers_candidate_by_primary_key(self):
        from unittest.mock import Mock

        submission = candidate("recover-delivery")
        client = Mock()
        client.get_item.side_effect = [
            {"Item": {**DynamoDbCandidateRepository._delivery_key("recover-delivery"),
                      "candidate_id": {"S": submission.candidate_id}}},
            {"Item": DynamoDbCandidateRepository._candidate_item(submission)},
        ]
        repository = DynamoDbCandidateRepository("candidates", dynamodb_client=client)

        recovered = await repository.get_for_delivery("recover-delivery")
        self.assertEqual(recovered.candidate_id, submission.candidate_id)
        self.assertTrue(client.get_item.call_args_list[0].kwargs["ConsistentRead"])
        self.assertTrue(client.get_item.call_args_list[1].kwargs["ConsistentRead"])

    async def test_dynamodb_analysis_result_float_round_trip_and_update(self):
        """A successful analysis must be writable, readable, then writable again."""
        from unittest.mock import Mock

        client = Mock()
        repository = DynamoDbCandidateRepository("candidates", dynamodb_client=client)
        analyzed = candidate("analysis-delivery").model_copy(update={
            "status": CandidateStatus.ACCEPTED,
            "analysis_decision": "PASS",
            "analysis_result": {
                "decision": "PASS", "confidence": 0.85,
                "evidence": ["app.py:L7 (source excerpt verified)"],
                "strengths": [], "issues": [], "missing_requirements": [],
                "revision_feedback": "", "recommendation": "Consider assignment.",
                "metrics": {"scores": [0.25, 1, -0.5]},
            },
        })

        await repository.update(analyzed)
        first_item = client.put_item.call_args.kwargs["Item"]
        self.assertEqual(first_item["analysis_result"]["M"]["confidence"], {"N": "0.85"})
        self.assertEqual(first_item["analysis_result"]["M"]["metrics"]["M"]["scores"]["L"],
                         [{"N": "0.25"}, {"N": "1"}, {"N": "-0.5"}])

        client.get_item.return_value = {"Item": first_item}
        restored = await repository.get(analyzed.candidate_id)
        self.assertEqual(restored.analysis_result["confidence"], 0.85)
        self.assertIsInstance(restored.analysis_result["confidence"], float)
        self.assertEqual(restored.analysis_result["metrics"]["scores"], [0.25, 1, -0.5])
        await repository.update(restored)
        self.assertEqual(client.put_item.call_args.kwargs["Item"], first_item)

    async def test_dynamodb_rejects_nonfinite_analysis_numbers(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "Non-finite numbers"):
                    DynamoDbCandidateRepository._serialize({"analysis": [value]})

    async def test_create_get_update_list_and_duplicate(self):
        repo=InMemoryCandidateRepository(); c=await repo.create(candidate())
        self.assertEqual((await repo.get(c.candidate_id)).candidate_id,c.candidate_id)
        self.assertEqual((await repo.update_status(c.candidate_id,CandidateStatus.FAILED)).status,CandidateStatus.FAILED)
        self.assertEqual(len(await repo.list_for_issue("o","r",1)),1); self.assertTrue(await repo.has_delivery("d"))
        with self.assertRaises(ValueError): await repo.create(candidate())

    async def test_dynamodb_keys_only_index_and_pagination(self):
        from unittest.mock import Mock
        client = Mock()
        first = candidate("delivery-1")
        second = candidate("delivery-2").model_copy(update={"comment_id": 2})
        client.query.side_effect = [
            {"Items": [{"pk": {"S": f"CANDIDATE#{first.candidate_id}"}, "sk": {"S": "CANDIDATE"}}],
             "LastEvaluatedKey": {"pk": {"S": "page-1"}}},
            {"Items": [{"pk": {"S": "STATE#o#r#1"}, "sk": {"S": "STATE"}},
                       {"pk": {"S": f"CANDIDATE#{second.candidate_id}"}, "sk": {"S": "CANDIDATE"}}]},
        ]
        client.get_item.side_effect = [
            {"Item": DynamoDbCandidateRepository._candidate_item(first)},
            {"Item": DynamoDbCandidateRepository._candidate_item(second)},
        ]
        repository = DynamoDbCandidateRepository("candidates", dynamodb_client=client)
        found = await repository.list_for_issue("o", "r", 1)
        self.assertEqual([item.comment_id for item in found], [1, 2])
        self.assertEqual(client.query.call_args_list[1].kwargs["ExclusiveStartKey"],
                         {"pk": {"S": "page-1"}})
        self.assertEqual(client.get_item.call_count, 2)

    async def test_find_by_comment_with_keys_only_index(self):
        from unittest.mock import Mock
        client = Mock()
        submission = candidate("delivery-3")
        client.query.return_value = {
            "Items": [{"pk": {"S": f"CANDIDATE#{submission.candidate_id}"},
                       "sk": {"S": "CANDIDATE"}}]
        }
        client.get_item.return_value = {
            "Item": DynamoDbCandidateRepository._candidate_item(submission)
        }
        repository = DynamoDbCandidateRepository("candidates", dynamodb_client=client)
        repository._issue_key_attribute = "issue-key"
        found = await repository.find_by_comment("o", "r", 1, submission.comment_id)
        self.assertEqual(found.candidate_id, submission.candidate_id)
        self.assertEqual(client.query.call_args.kwargs["ExpressionAttributeNames"],
                         {"#issue_key": "issue-key"})


class DynamoDbConfigurationTests(unittest.TestCase):
    def test_documented_aws_region_is_passed_to_boto3(self):
        with patch.dict(os.environ, {"AWS_REGION": "ap-south-1"}, clear=True), \
             patch("boto3.Session") as create_session:
            repository = DynamoDbCandidateRepository("candidates")
        create_session.return_value.get_credentials.assert_called_once_with()
        create_session.return_value.client.assert_called_once_with("dynamodb", region_name="ap-south-1")
        self.assertIs(repository._client, create_session.return_value.client.return_value)

    def test_boto3_default_region_or_profile_remains_available(self):
        with patch.dict(os.environ, {"AWS_DEFAULT_REGION": "us-east-1"}, clear=True), \
             patch("boto3.Session") as create_session:
            DynamoDbCandidateRepository("candidates")
        create_session.return_value.client.assert_called_once_with("dynamodb", region_name=None)

    def test_missing_region_has_actionable_error(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch("boto3.Session") as create_session:
            create_session.return_value.client.side_effect = NoRegionError()
            with self.assertRaisesRegex(DynamoDbConfigurationError, "AWS_REGION or AWS_DEFAULT_REGION"):
                DynamoDbCandidateRepository("candidates")

    def test_missing_credentials_fail_before_first_dynamodb_request(self):
        with patch.dict(os.environ, {"AWS_REGION": "ap-south-1"}, clear=True), \
             patch("boto3.Session") as create_session:
            create_session.return_value.get_credentials.return_value = None
            with self.assertRaisesRegex(DynamoDbConfigurationError, "AWS credentials are not configured"):
                DynamoDbCandidateRepository("candidates")
        create_session.return_value.client.assert_not_called()

    def test_invalid_profile_has_actionable_error(self):
        with patch.dict(os.environ, {"AWS_PROFILE": "missing", "AWS_REGION": "ap-south-1"}, clear=True), \
             patch("boto3.Session") as create_session:
            create_session.return_value.get_credentials.side_effect = ProfileNotFound(profile="missing")
            with self.assertRaisesRegex(DynamoDbConfigurationError, "AWS_PROFILE"):
                DynamoDbCandidateRepository("candidates")

    def test_injected_client_needs_no_aws_region(self):
        fake_client = object()
        with patch.dict(os.environ, {}, clear=True):
            repository = DynamoDbCandidateRepository("candidates", dynamodb_client=fake_client)
        self.assertIs(repository._client, fake_client)

    def test_startup_read_check_does_not_write(self):
        from unittest.mock import Mock
        client = Mock()
        repository = DynamoDbCandidateRepository("candidates", dynamodb_client=client)
        repository.check_read_access()
        client.get_item.assert_called_once_with(
            TableName="candidates",
            Key={"pk": {"S": "DELIVERY#__issuematch_startup_check__"}, "sk": {"S": "DELIVERY"}},
            ProjectionExpression="pk",
        )
        client.put_item.assert_not_called()
        client.query.assert_called_once()

    def test_hyphenated_index_key_is_detected_and_used_for_queries(self):
        from unittest.mock import Mock
        client = Mock()
        client.query.side_effect = [
            ClientError(
                {"Error": {"Code": "ValidationException", "Message":
                           "Query condition missed key schema element: issue-key"}}, "Query"
            ),
            {"Count": 0},
            {"Items": []},
            {"Items": []},
        ]
        repository = DynamoDbCandidateRepository("candidates", dynamodb_client=client)
        repository.check_read_access()
        self.assertEqual(repository._issue_key_attribute, "issue-key")
        self.assertEqual(client.query.call_args_list[1].kwargs["ExpressionAttributeNames"],
                         {"#issue_key": "issue-key"})
        self.assertEqual(client.query.call_args_list[1].kwargs["Select"], "COUNT")
        import asyncio
        asyncio.run(repository.list_for_issue("o", "r", 1))
        asyncio.run(repository.find_by_comment("o", "r", 1, 1))
        for call in client.query.call_args_list[2:]:
            self.assertEqual(call.kwargs["ExpressionAttributeNames"], {"#issue_key": "issue-key"})

    def test_candidate_and_issue_state_write_both_supported_index_keys(self):
        from unittest.mock import Mock
        from app.models.candidate import IssueEvaluation
        client = Mock()
        repository = DynamoDbCandidateRepository("candidates", dynamodb_client=client)
        item = repository._candidate_item(candidate())
        self.assertEqual(item["issue_key"], item["issue-key"])
        import asyncio
        asyncio.run(repository.update_issue_state(
            IssueEvaluation(repository_owner="o", repository_name="r", issue_number=1)
        ))
        state_item = client.put_item.call_args.kwargs["Item"]
        self.assertEqual(state_item["issue_key"], state_item["issue-key"])

    def test_unsupported_index_key_fails_at_startup(self):
        from unittest.mock import Mock
        client = Mock()
        client.query.side_effect = [
            ClientError({"Error": {"Code": "ValidationException", "Message":
                                   "Query condition missed key schema element: other"}}, "Query"),
            ClientError({"Error": {"Code": "ValidationException", "Message":
                                   "Query condition missed key schema element: other"}}, "Query"),
        ]
        repository = DynamoDbCandidateRepository("candidates", dynamodb_client=client)
        with self.assertRaisesRegex(DynamoDbConfigurationError, "unsupported key schema"):
            repository.check_read_access()

    def test_startup_read_check_reports_permission_denied(self):
        from unittest.mock import Mock
        client = Mock()
        client.get_item.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "GetItem"
        )
        repository = DynamoDbCandidateRepository("candidates", dynamodb_client=client)
        with self.assertRaisesRegex(DynamoDbConfigurationError, "dynamodb:GetItem"):
            repository.check_read_access()

    def test_startup_read_check_reports_wrong_table_or_region(self):
        from unittest.mock import Mock
        client = Mock()
        client.get_item.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "missing"}}, "GetItem"
        )
        repository = DynamoDbCandidateRepository("candidates", dynamodb_client=client)
        with self.assertRaisesRegex(DynamoDbConfigurationError, "region/account"):
            repository.check_read_access()
