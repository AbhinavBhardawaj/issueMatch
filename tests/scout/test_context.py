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


# ===========================================================================
# PHASE 3: CONTEXT BOUNDS & COMPLETENESS REGRESSION TESTS
# ===========================================================================

@pytest.mark.asyncio
async def test_changed_files_exceeding_max_marks_partial():
    """When changed files exceed MAX_CHANGED_FILES, context must be marked PARTIAL with CHANGED_FILE_LIMIT_REACHED."""
    client = FakeScoutReadClient()
    # Add 100 files to client
    for i in range(100):
        client.files[f"app/module_{i}.py"] = f"x = {i}\n"

    builder = ScoutContextBuilder(client)
    event = make_test_event()
    changed = [ChangedFile(path=f"app/module_{i}.py", status="modified") for i in range(100)]

    ctx = await builder.build_context(event, changed)
    assert len(ctx.files) <= 25
    assert ctx.context_completeness == ContextCompleteness.PARTIAL
    assert "CHANGED_FILE_LIMIT_REACHED" in ctx.partial_reasons


@pytest.mark.asyncio
async def test_test_files_exceeding_max_marks_partial():
    """All test files (changed or discovered) must respect MAX_TEST_FILES and mark PARTIAL."""
    client = FakeScoutReadClient()
    for i in range(15):
        client.files[f"tests/test_{i}.py"] = f"def test_{i}(): pass\n"

    builder = ScoutContextBuilder(client)
    event = make_test_event()
    # 15 changed test files (exceeding MAX_TEST_FILES = 10)
    changed = [ChangedFile(path=f"tests/test_{i}.py", status="modified") for i in range(15)]

    ctx = await builder.build_context(event, changed)
    assert len(ctx.test_files) <= 10
    assert ctx.context_completeness == ContextCompleteness.PARTIAL
    assert "TEST_FILE_LIMIT_REACHED" in ctx.partial_reasons


@pytest.mark.asyncio
async def test_safe_utf8_byte_truncation_multibyte_characters():
    """Safe UTF-8 byte truncation must not split multibyte code points and must never exceed byte budget."""
    client = FakeScoutReadClient()
    # 4-byte emoji repeated 6000 times = 24,000 bytes > MAX_FILE_BYTES (20,000)
    multibyte_content = "🚀" * 6000
    client.files["app/unicode.py"] = multibyte_content

    builder = ScoutContextBuilder(client)
    event = make_test_event()
    changed = [ChangedFile(path="app/unicode.py", status="modified")]

    ctx = await builder.build_context(event, changed)
    unicode_file = next(f for f in ctx.files if f.path == "app/unicode.py")
    assert unicode_file.truncated is True
    assert len(unicode_file.content.encode("utf-8")) <= 20_000
    # Must be valid UTF-8 without replacement char '\ufffd'
    assert "\ufffd" not in unicode_file.content
    assert ctx.context_completeness == ContextCompleteness.PARTIAL
    assert "FILE_TRUNCATED" in ctx.partial_reasons


@pytest.mark.asyncio
async def test_readme_truncation_in_bytes_marks_partial():
    """README exceeding MAX_FILE_BYTES in UTF-8 bytes must be truncated safely and mark PARTIAL."""
    client = FakeScoutReadClient()
    # 3-byte Japanese kanji repeated 8000 times = 24,000 bytes > 20,000
    client.files["README.md"] = "日本語" * 8000

    builder = ScoutContextBuilder(client)
    event = make_test_event()
    changed = [ChangedFile(path="app/main.py", status="modified")]

    ctx = await builder.build_context(event, changed)
    assert len(ctx.readme.encode("utf-8")) <= 20_000
    assert "\ufffd" not in ctx.readme
    assert ctx.context_completeness == ContextCompleteness.PARTIAL
    assert "README_TRUNCATED" in ctx.partial_reasons
