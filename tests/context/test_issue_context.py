import unittest
from app.context.issue_context import collect_issue_context
from tests.fakes import FakeGitHubClient
class IssueContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_normalizes_missing_body(self):
        context=await collect_issue_context(FakeGitHubClient(),"o","r",1)
        self.assertEqual(context.body,""); self.assertEqual(context.labels,["bug"])
