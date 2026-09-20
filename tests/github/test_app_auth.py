import os, sys, types, unittest
from unittest.mock import patch
import httpx
from app.github.app_auth import GitHubAppAuthenticator, GitHubAppConfig, GitHubConfigurationError

class AuthTests(unittest.TestCase):
    def test_missing_config_is_clear(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(GitHubConfigurationError): GitHubAppConfig.from_environment()

    def test_reads_config_without_real_credentials(self):
        pem_stub = "-----BEGIN RSA PRIVATE KEY-----\\nfake-key-data\\n-----END RSA PRIVATE KEY-----"
        expected = "-----BEGIN RSA PRIVATE KEY-----\nfake-key-data\n-----END RSA PRIVATE KEY-----"
        with patch.dict(os.environ, {"GITHUB_APP_ID":"1", "GITHUB_PRIVATE_KEY":pem_stub, "GITHUB_WEBHOOK_SECRET":"secret"}, clear=True):
            self.assertEqual(GitHubAppConfig.from_environment().private_key, expected)

    def test_inline_pem_escaped_newlines_parsed_to_actual_newlines(self):
        """A. Inline PEM with literal escaped newlines results in actual newline characters."""
        pem_escaped = "-----BEGIN RSA PRIVATE KEY-----\\nline1\\nline2\\n-----END RSA PRIVATE KEY-----"
        expected = "-----BEGIN RSA PRIVATE KEY-----\nline1\nline2\n-----END RSA PRIVATE KEY-----"
        with patch.dict(os.environ, {"GITHUB_APP_ID": "1", "GITHUB_PRIVATE_KEY": pem_escaped, "GITHUB_WEBHOOK_SECRET": "secret"}, clear=True):
            config = GitHubAppConfig.from_environment()
            self.assertEqual(config.private_key, expected)
            self.assertIn("\n", config.private_key)
            self.assertNotIn("\\n", config.private_key)

    def test_pem_file_path_reads_file_content(self):
        """B. PEM FILE PATH: config.private_key equals file content, not the pathname."""
        import tempfile
        from pathlib import Path
        file_content = "-----BEGIN RSA PRIVATE KEY-----\nfake-key-data\n-----END RSA PRIVATE KEY-----\n"
        with tempfile.TemporaryDirectory() as tmp_dir:
            key_file = Path(tmp_dir) / "test_key.pem"
            key_file.write_text(file_content, encoding="utf-8")
            with patch.dict(os.environ, {"GITHUB_APP_ID": "1", "GITHUB_PRIVATE_KEY": str(key_file), "GITHUB_WEBHOOK_SECRET": "secret"}, clear=True):
                config = GitHubAppConfig.from_environment()
                self.assertEqual(config.private_key, file_content)
                self.assertNotEqual(config.private_key, str(key_file))

    def test_invalid_private_key_value_raises_configuration_error_without_leakage(self):
        """C & D. Invalid value raises GitHubConfigurationError and error string does NOT leak key contents."""
        fake_secret_key = "super-secret-unparseable-private-key-material"
        with patch.dict(os.environ, {"GITHUB_APP_ID": "1", "GITHUB_PRIVATE_KEY": fake_secret_key, "GITHUB_WEBHOOK_SECRET": "secret"}, clear=True):
            with self.assertRaises(GitHubConfigurationError) as exc_info:
                GitHubAppConfig.from_environment()
            error_str = str(exc_info.exception)
            self.assertNotIn(fake_secret_key, error_str)

    def test_invalid_pem_file_does_not_leak_file_contents(self):
        """D. Invalid PEM file content does NOT leak in exception error string."""
        import tempfile
        from pathlib import Path
        secret_file_content = "confidential-non-pem-file-bytes"
        with tempfile.TemporaryDirectory() as tmp_dir:
            bad_file = Path(tmp_dir) / "bad_key.pem"
            bad_file.write_text(secret_file_content, encoding="utf-8")
            with patch.dict(os.environ, {"GITHUB_APP_ID": "1", "GITHUB_PRIVATE_KEY": str(bad_file), "GITHUB_WEBHOOK_SECRET": "secret"}, clear=True):
                with self.assertRaises(GitHubConfigurationError) as exc_info:
                    GitHubAppConfig.from_environment()
                error_str = str(exc_info.exception)
                self.assertNotIn(secret_file_content, error_str)
            
class InstallationTokenTests(unittest.IsolatedAsyncioTestCase):
    async def test_installation_token_is_requested_with_mocked_auth(self):
        async def handler(request): 
            return httpx.Response(201, json={"token":"mock-token"}, request=request)

        auth=GitHubAppAuthenticator(GitHubAppConfig("1","unused","secret"), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with patch.object(auth, "create_app_jwt", return_value="mock-jwt"):
            self.assertEqual(await auth.get_installation_token(2), "mock-token")

class ProductionCompositionTests(unittest.TestCase):
    def test_memory_demo_composes_real_dependency_graph(self):
        from tests.fakes import FakeAnalysisService
        fake_factory = types.ModuleType("app.analysis.factory")
        fake_factory.create_analysis_service = FakeAnalysisService
        pem_stub = "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----"
        environment = {"GITHUB_APP_ID": "1", "GITHUB_PRIVATE_KEY": pem_stub, "GITHUB_WEBHOOK_SECRET": "secret", "ISSUEMATCH_STORAGE": "memory"}
        with patch.dict(os.environ, environment, clear=True), patch.dict(sys.modules, {"app.analysis.factory": fake_factory}):
            from app.main import create_production_app
            app = create_production_app()
        self.assertIn("/webhooks/github", app.openapi()["paths"])
