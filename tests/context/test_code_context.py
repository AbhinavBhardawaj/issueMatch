import unittest
from app.context.code_context import collect_code_context
from app.context.repository_context import collect_repository_context
from tests.fakes import FakeGitHubClient
class CodeContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_missing_and_bounded(self):
        client=FakeGitHubClient(files={"src/auth/session.py":"x"*20000,"tests/test_session.py":"test"})
        repository=await collect_repository_context(client,"o","r")
        result=await collect_code_context(client,"o","r","modify src/auth/session.py and src/nope.py",repository)
        self.assertIn("src/auth/session.py",result.relevant_files); self.assertIn("src/nope.py",result.missing_paths)
        self.assertLessEqual(result.total_characters,40000); self.assertTrue(result.file_contents[0].truncated)
