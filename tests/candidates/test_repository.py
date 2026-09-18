import unittest
from app.candidates.repository import InMemoryCandidateRepository
from app.models.candidate import CandidateStatus, CandidateSubmission
def candidate(delivery="d"):
    return CandidateSubmission(repository_owner="o",repository_name="r",issue_number=1,contributor_username="u",comment_id=1,comment_body="x",approach="x",webhook_delivery_id=delivery)
class RepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_get_update_list_and_duplicate(self):
        repo=InMemoryCandidateRepository(); c=await repo.create(candidate())
        self.assertEqual((await repo.get(c.candidate_id)).candidate_id,c.candidate_id)
        self.assertEqual((await repo.update_status(c.candidate_id,CandidateStatus.FAILED)).status,CandidateStatus.FAILED)
        self.assertEqual(len(await repo.list_for_issue("o","r",1)),1); self.assertTrue(await repo.has_delivery("d"))
        with self.assertRaises(ValueError): await repo.create(candidate())
