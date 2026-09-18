import base64, unittest
import httpx
from app.github.client import GitHubNotFoundError, GitHubRestClient
class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_decodes_file_and_handles_not_found(self):
        async def handler(request):
            if "/issues/1" in request.url.path: return httpx.Response(404, request=request)
            return httpx.Response(200, json={"type":"file", "content":base64.b64encode(b"ok").decode()}, request=request)
        client = GitHubRestClient("not-logged", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        self.assertEqual(await client.get_file_content("o","r","x.py","main"), "ok")
        with self.assertRaises(GitHubNotFoundError): await client.get_issue("o","r",1)
