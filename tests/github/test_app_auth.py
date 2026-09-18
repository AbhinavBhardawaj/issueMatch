import os, unittest
from unittest.mock import patch
import httpx
from app.github.app_auth import GitHubAppAuthenticator, GitHubAppConfig, GitHubConfigurationError

class AuthTests(unittest.TestCase):
    def test_missing_config_is_clear(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(GitHubConfigurationError): GitHubAppConfig.from_environment()

    def test_reads_config_without_real_credentials(self):
        with patch.dict(os.environ, {"GITHUB_APP_ID":"1", "GITHUB_PRIVATE_KEY":"key\\nvalue", "GITHUB_WEBHOOK_SECRET":"secret"}, clear=True):
            self.assertEqual(GitHubAppConfig.from_environment().private_key, "key\nvalue")
            
class InstallationTokenTests(unittest.IsolatedAsyncioTestCase):
    async def test_installation_token_is_requested_with_mocked_auth(self):
        async def handler(request): 
            return httpx.Response(201, json={"token":"mock-token"}, request=request)

        auth=GitHubAppAuthenticator(GitHubAppConfig("1","unused","secret"), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with patch.object(auth, "create_app_jwt", return_value="mock-jwt"):
            self.assertEqual(await auth.get_installation_token(2), "mock-token")
