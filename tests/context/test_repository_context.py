import unittest
from app.context.repository_context import collect_repository_context
from tests.fakes import FakeGitHubClient

class RepositoryContextTests(unittest.IsolatedAsyncioTestCase):
    
    async def test_collects_bounded_metadata(self):
        context=await collect_repository_context(FakeGitHubClient(),"o","r")
        self.assertEqual(context.default_branch,"main"); self.assertIn("src",context.top_level_entries)
