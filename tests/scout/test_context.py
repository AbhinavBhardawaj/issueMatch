import pytest
from app.github.events import GitHubPushEvent
from app.scout.models import ChangedFile, ContextCompleteness
from app.scout.context import ScoutContextBuilder, _is_supported_path, _extract_imports
from app.github.scout_repository import verify_repository_identity, SecurityIdentityError


class FakeScoutReadClient:
    def __init__(self, repo_id=12345, truncated_tree=False):
        self.repo_id = repo_id
        self.truncated_tree = truncated_tree
        self.files = {
            "app/main.py": "import os\nfrom app.utils import helper\nprint('hello')",
            "app/utils.py": "def helper():\n    return 42",
            "tests/test_main.py": "def test_hello():\n    assert True",
            "README.md": "# Project Title\nDocumentation text.",
        }

    async def get_repository(self, owner: str, repo: str):
        return {"id": self.repo_id, "name": repo, "owner": {"login": owner}}

    async def compare_commits(self, owner: str, repo: str, before_sha: str, after_sha: str):
        return {"files": [{"filename": "app/main.py", "status": "modified"}]}

    async def get_repository_tree(self, owner: str, repo: str, ref: str):
        items = [{"path": k, "type": "blob"} for k in self.files.keys()]
        return items, self.truncated_tree

    async def get_file_content(self, owner: str, repo: str, path: str, ref: str):
        if path in self.files:
            return self.files[path]
        raise RuntimeError("File not found")

    async def get_issues(self, owner: str, repo: str, state: str = "open", per_page: int = 50):
        return [{"number": 1, "title": "Existing Bug", "state": "open", "body": "Summary text"}]


def make_test_event():
    return GitHubPushEvent(
        delivery_id="d1",
        installation_id=100,
        repository_id=12345,
        repository_owner="org",
        repository_name="repo",
        default_branch="main",
        before_sha="a" * 40,
        after_sha="b" * 40,
        ref="refs/heads/main",
    )


@pytest.mark.asyncio
async def test_repository_identity_success():
    client = FakeScoutReadClient(repo_id=12345)
    data = await verify_repository_identity(client, "org", "repo", 12345)
    assert data["id"] == 12345


@pytest.mark.asyncio
async def test_repository_identity_mismatch_fails_closed():
    client = FakeScoutReadClient(repo_id=99999)
    with pytest.raises(SecurityIdentityError, match="Repository ID mismatch"):
        await verify_repository_identity(client, "org", "repo", 12345)


@pytest.mark.asyncio
async def test_scout_context_builder():
    client = FakeScoutReadClient()
    builder = ScoutContextBuilder(client)
    event = make_test_event()
    changed = [ChangedFile(path="app/main.py", status="modified")]

    ctx = await builder.build_context(event, changed)
    assert ctx.repository_id == 12345
    assert ctx.commit_sha == "b" * 40
    assert len(ctx.files) >= 1
    assert any(f.path == "app/main.py" for f in ctx.files)
    assert len(ctx.test_files) >= 1
    assert ctx.readme != ""
    assert len(ctx.existing_issues) == 1
    assert ctx.context_completeness == ContextCompleteness.COMPLETE


@pytest.mark.asyncio
async def test_truncated_tree_sets_partial_context():
    client = FakeScoutReadClient(truncated_tree=True)
    builder = ScoutContextBuilder(client)
    event = make_test_event()
    changed = [ChangedFile(path="app/main.py", status="modified")]

    ctx = await builder.build_context(event, changed, tree_truncated=True)
    assert ctx.context_completeness == ContextCompleteness.PARTIAL
    assert "TREE_TRUNCATED" in ctx.partial_reasons


def test_supported_extensions():
    assert _is_supported_path("test.py") is True
    assert _is_supported_path("src/index.ts") is True
    assert _is_supported_path("node_modules/package.json") is False
    assert _is_supported_path(".git/HEAD") is False
    assert _is_supported_path("app.png") is False


def test_import_extraction_python():
    code = "import os\nfrom app.services import pipeline\nimport app.domain.models as models"
    imports = _extract_imports("app/main.py", code)
    assert "pipeline.py" in imports
    assert "models.py" in imports
