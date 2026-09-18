import hashlib, hmac, json, unittest
from fastapi.testclient import TestClient
from app.candidates.repository import InMemoryCandidateRepository
from app.candidates.service import CandidateService
from app.github.webhook import create_github_webhook_router
from app.main import create_app
from tests.fakes import FakeGitHubClient, FakeAnalysisService

def body(action="created"):
    return json.dumps({"action":action,"repository":{"name":"repo","owner":{"login":"org"}},"issue":{"number":1,"title":"x"},"comment":{"id":1,"body":"thanks","user":{"login":"u"}}}).encode()
class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.repository=InMemoryCandidateRepository(); self.analyzer=FakeAnalysisService()
        service=CandidateService(self.repository, lambda _: FakeGitHubClient(), self.analyzer)
        self.client=TestClient(create_app("secret", service))
    def _headers(self, raw, signature=True, event="issue_comment"):
        headers={"X-GitHub-Event":event,"X-GitHub-Delivery":"d","content-type":"application/json"}
        if signature: headers["X-Hub-Signature-256"]="sha256="+hmac.new(b"secret",raw,hashlib.sha256).hexdigest()
        return headers
    def test_valid_raw_signature_accepted(self): self.assertEqual(self.client.post("/webhooks/github",content=body(),headers=self._headers(body())).status_code,202)
    def test_created_issue_comment_is_processed(self):
        raw=body().replace(b"thanks", b"I'd like to work on this. I will modify src/auth/session.py")
        self.assertEqual(self.client.post("/webhooks/github",content=raw,headers=self._headers(raw)).status_code,202)
        self.assertEqual(len(self.analyzer.calls), 1)
    def test_invalid_or_missing_signature_rejected(self):
        raw=body(); self.assertEqual(self.client.post("/webhooks/github",content=raw,headers=self._headers(raw,False)).status_code,401)
        self.assertEqual(self.client.post("/webhooks/github",content=raw,headers=self._headers(b"other")).status_code,401)
    def test_unsupported_ignored_and_malformed_rejected(self):
        raw=body(); self.assertEqual(self.client.post("/webhooks/github",content=raw,headers=self._headers(raw,event="push")).json()["status"],"ignored")
        raw=b"{"; self.assertEqual(self.client.post("/webhooks/github",content=raw,headers=self._headers(raw)).status_code,400)
