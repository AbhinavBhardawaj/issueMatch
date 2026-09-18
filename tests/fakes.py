from typing import Any
from app.models.analysis import AnalysisDecision, ApproachAnalysis

class FakeGitHubClient:
    def __init__(self, fail: bool = False, files: dict[str, str] | None = None): 
        self.fail, self.files = fail, files or {"src/auth/session.py": "def timeout(): pass", "tests/test_session.py": "def test_timeout(): pass"}

    def _check(self):
        if self.fail: raise RuntimeError("GitHub unavailable")

    async def get_issue(self, *args): self._check(); return {"title": "Session timeout", "body": None, "labels": [{"name": "bug"}], "state": "open", "user": {"login": "maintainer", "id": 2}}
    async def get_repository(self, *args): self._check(); return {"default_branch": "main", "description": "demo"}
    async def get_languages(self, *args): self._check(); return {"Python": 20}
    async def get_repository_tree(self, *args): self._check(); return ([{"type": "blob", "path": p} for p in self.files], False)
    async def get_file_content(self, owner, repo, path, ref): self._check(); return self.files[path]
    async def create_issue_comment(self, *args): return {"id": 1}

class FakeAnalysisService:
    def __init__(self, fail=False): self.calls = []; self.fail = fail

    async def analyze(self, candidate, context):
        self.calls.append((candidate, context))
        if self.fail: 
            raise RuntimeError("analysis unavailable")
        
        return ApproachAnalysis(decision=AnalysisDecision.PASS, confidence=.9, evidence=["src/auth/session.py"], recommendation="Maintainer may assign this issue.")

def event(delivery="d1", comment="I'd like to work on this. I will modify src/auth/session.py and update tests/test_session.py."):

    from app.github.events import GitHubIssueCommentEvent
    return GitHubIssueCommentEvent(delivery_id=delivery, repository_owner="acme", repository_name="demo", installation_id=1, issue_number=7, issue_title="Session timeout", comment_id=9, comment_body=comment, comment_author="alice", repository_default_branch="main")
