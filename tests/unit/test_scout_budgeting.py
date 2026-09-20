import json
import pytest
import httpx
from unittest.mock import AsyncMock, patch

from app.github.events import GitHubPushEvent
from app.scout.models import (
    ScoutContext,
    ScoutFile,
    ScoutExistingIssue,
    ChangedFile,
    ContextCompleteness,
)
from app.scout.context import (
    ScoutContextBuilder,
    MAX_FILE_BYTES,
    MAX_PATCH_BYTES,
    MAX_TOTAL_PATCH_BYTES,
    MAX_README_BYTES,
    MAX_ISSUES_TOTAL_BYTES,
    MAX_TEST_FILES,
    MAX_TOTAL_CONTEXT_BYTES,
    truncate_utf8_bytes,
    _rank_relevant_test_files,
)
from app.scout.prompts import (
    SYSTEM_PROMPT,
    MAX_SCOUT_REQUEST_BYTES,
    build_scout_prompt,
    estimate_serialized_request_size,
)
from app.scout.worthiness import (
    validate_and_filter_drafts_for_context,
    rank_and_cap_push_findings,
    rank_and_filter_drafts,
)
from app.scout.schemas import ScoutFindingDraft, ScoutEvidenceDraft
from app.scout.service import partition_changed_files_for_scout
from app.scout.agent import ScoutAgent, MalformedScoutResponse
from app.infrastructure.llm_router import GroqProvider
from app.verifier.schemas import LLMInvocationError


def _make_push_event(after_sha: str = "abcdef1234567890123456789012345678901234") -> GitHubPushEvent:
    return GitHubPushEvent(
        delivery_id="deliv-123",
        installation_id=1001,
        repository_id=12345,
        repository_owner="test-owner",
        repository_name="test-repo",
        default_branch="main",
        before_sha="0" * 40,
        after_sha=after_sha,
        ref="refs/heads/main",
        forced=False,
        deleted=False,
    )


class TestLayer1ContextBudgeting:
    def test_truncate_utf8_bytes_safe(self):
        # 4-byte emoji: 🌍 is 4 bytes in utf-8
        text = "Hello 🌍 world"
        # "Hello " is 6 bytes. 6 + 2 bytes cuts inside 🌍 -> must discard partial code point
        truncated, was_trunc = truncate_utf8_bytes(text, 8)
        assert was_trunc is True
        assert truncated == "Hello "
        assert len(truncated.encode("utf-8")) <= 8

    @pytest.mark.asyncio
    async def test_patch_capping_and_flag(self):
        mock_client = AsyncMock()
        mock_client.get_file_content.return_value = "def hello(): pass\n"
        mock_client.get_repository_tree.return_value = ([], False)
        mock_client.list_recent_closed_issues.return_value = []

        builder = ScoutContextBuilder(client=mock_client)
        huge_patch = "A" * 15_000
        cf = ChangedFile(path="app/big.py", patch=huge_patch)

        ctx = await builder.build_context(_make_push_event(), [cf])
        assert len(ctx.files) == 1
        assert ctx.files[0].patch is not None
        assert len(ctx.files[0].patch.encode("utf-8")) <= MAX_PATCH_BYTES
        assert ctx.context_completeness == ContextCompleteness.PARTIAL
        assert "PATCH_TRUNCATED" in ctx.partial_reasons

    @pytest.mark.asyncio
    async def test_per_batch_patch_budget(self):
        mock_client = AsyncMock()
        mock_client.get_file_content.return_value = "pass\n"
        mock_client.get_repository_tree.return_value = ([], False)
        mock_client.list_recent_closed_issues.return_value = []

        builder = ScoutContextBuilder(client=mock_client)
        # 5 changed files, each with 5KB patch -> 25KB total > MAX_TOTAL_PATCH_BYTES (20KB)
        changed_files = [
            ChangedFile(path=f"app/mod_{i}.py", patch="+" + ("x" * 5_000))
            for i in range(5)
        ]

        ctx = await builder.build_context(_make_push_event(), changed_files)
        total_patch_bytes = sum(len(f.patch.encode("utf-8")) for f in ctx.files if f.patch)
        assert total_patch_bytes <= MAX_TOTAL_PATCH_BYTES
        assert ctx.context_completeness == ContextCompleteness.PARTIAL
        assert "PATCH_TRUNCATED" in ctx.partial_reasons

    @pytest.mark.asyncio
    async def test_huge_readme_bounded(self):
        mock_client = AsyncMock()
        mock_client.get_file_content.side_effect = lambda owner, repo, path, ref: (
            "# Readme\n" + ("R" * 25_000) if path.lower() == "readme.md" else "pass\n"
        )
        mock_client.get_repository_tree.return_value = (
            [{"path": "README.md", "type": "blob"}],
            False,
        )
        mock_client.list_recent_closed_issues.return_value = []

        builder = ScoutContextBuilder(client=mock_client)
        cf = ChangedFile(path="app/main.py", patch="+ pass\n")
        ctx = await builder.build_context(_make_push_event(), [cf])

        assert len(ctx.readme.encode("utf-8")) <= MAX_README_BYTES
        assert ctx.context_completeness == ContextCompleteness.PARTIAL
        assert "README_TRUNCATED" in ctx.partial_reasons

    def test_unrelated_test_exclusion_and_ranking(self):
        changed_paths = ["app/auth/login.py", "app/models/user.py"]
        available_tests = [
            "tests/unrelated/test_billing.py",
            "tests/test_user.py",              # Tier 2 (matching stem)
            "tests/auth/test_login.py",         # Tier 2 (matching stem)
            "tests/integration/test_all.py",    # Unrelated
            "tests/e2e/test_checkout.py",       # Unrelated
            "tests/unit/test_random_widget.py", # Unrelated
        ]
        potential_imports = {"user.py", "app/models/user.py"}

        ranked = _rank_relevant_test_files(
            candidate_paths=available_tests,
            changed_paths=changed_paths,
            potential_imports=potential_imports,
        )
        # Should retain only relevant tests
        assert "tests/auth/test_login.py" in ranked
        assert "tests/test_user.py" in ranked
        assert "tests/unrelated/test_billing.py" not in ranked
        assert "tests/integration/test_all.py" not in ranked
        assert "tests/e2e/test_checkout.py" not in ranked
        assert "tests/unit/test_random_widget.py" not in ranked


class TestLayer2PromptBudgeting:
    def test_estimate_serialized_request_size(self):
        user = "Hello Scout"
        system = "You are Scout"
        size = estimate_serialized_request_size(user, system)
        payload = {
            "model": "qwen/qwen3.8-27b",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        expected = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        assert size == expected

    def test_prompt_reduction_enforces_budget(self):
        # Create a large context
        files = [
            ScoutFile(
                path=f"app/file_{i}.py",
                content="x = 1\n" * 400,
                changed=True,
                patch="+ x = 1\n" * 200,
                size_bytes=len(("x = 1\n" * 400).encode("utf-8")),
            )
            for i in range(5)
        ]
        tests = [
            ScoutFile(
                path=f"tests/test_file_{i}.py",
                content="def test(): pass\n" * 200,
                changed=False,
                size_bytes=len(("def test(): pass\n" * 200).encode("utf-8")),
            )
            for i in range(3)
        ]
        readme = "# Readme\n" + ("info\n" * 200)
        issues = [
            ScoutExistingIssue(number=i, title=f"Issue #{i}: crash", body_summary="bug details")
            for i in range(10)
        ]
        changed = [f"app/file_{i}.py" for i in range(5)]

        ctx = ScoutContext(
            installation_id=1,
            repository_id=123,
            owner="test-owner",
            name="test-repo",
            before_sha="0" * 40,
            commit_sha="1" * 40,
            files=files,
            test_files=tests,
            readme=readme,
            existing_issues=issues,
            changed_paths=changed,
            total_context_bytes=100_000,
            context_completeness=ContextCompleteness.COMPLETE,
        )

        event = _make_push_event()
        prompt = build_scout_prompt(event, ctx, max_request_bytes=MAX_SCOUT_REQUEST_BYTES)
        request_size = estimate_serialized_request_size(prompt, SYSTEM_PROMPT)

        assert request_size <= MAX_SCOUT_REQUEST_BYTES


class TestChangedFileBatching:
    def test_partition_20_changed_files(self):
        changed_files = [
            ChangedFile(path=f"app/module_{i}.py", patch="+" + ("x" * 2_000))
            for i in range(20)
        ]

        batches = partition_changed_files_for_scout(changed_files, max_files_per_batch=5, max_estimated_bytes_per_batch=15_000)
        assert len(batches) > 1

        # Check every file is assigned exactly once
        flat = [cf.path for b in batches for cf in b]
        assert sorted(flat) == sorted(cf.path for cf in changed_files)
        assert len(flat) == len(changed_files)

    def test_single_pathological_file_partition(self):
        changed_files = [ChangedFile(path="app/giant.py", patch="+" + ("x" * 50_000))]

        batches = partition_changed_files_for_scout(changed_files)
        assert len(batches) == 1
        assert batches[0][0].path == "app/giant.py"


def _make_scout_draft(
    title: str = "Auth bug",
    file: str = "app/auth.py",
    line: int = 1,
    snippet: str = "class Auth: pass",
    confidence: float = 0.9,
    impact: str = "meaningful",
    category: str = "behavioral_bug",
) -> ScoutFindingDraft:
    return ScoutFindingDraft(
        category=category,
        title=title,
        severity="high",
        file=file,
        line=line,
        description="Auth validation bug",
        expected_behavior="Should authenticate properly",
        evidence=[ScoutEvidenceDraft(file=file, line=line, snippet=snippet)],
        confidence=confidence,
        impact=impact,
        impact_reason="Users cannot log in",
        observable_behavior="500 Internal Server Error",
        affected_user_or_system="All users",
        actionable=True,
        actionability_reason="Direct logic bug in auth validator",
        regression_likelihood="introduced_by_push",
        expected_behavior_basis="test",
        expected_behavior_evidence="test_auth passes previously",
        claim_scope="local",
        depends_on_absence=False,
    )


class TestTwoPhaseWorthiness:
    def test_per_context_validation_rejects_unrelated_paths(self):
        auth_file = ScoutFile(
            path="app/auth.py",
            content="class Auth: pass\n",
            size_bytes=17,
            changed=True,
        )
        ctx_batch = ScoutContext(
            installation_id=1,
            repository_id=123,
            owner="test-owner",
            name="test-repo",
            before_sha="0" * 40,
            commit_sha="1" * 40,
            files=[auth_file],
            changed_paths=["app/auth.py"],
        )
        draft_valid = _make_scout_draft(
            title="Auth bug",
            file="app/auth.py",
            snippet="class Auth: pass",
            confidence=0.9,
        )
        draft_unrelated = _make_scout_draft(
            title="Database bug",
            file="app/db/engine.py",
            snippet="class Auth: pass",
            confidence=0.95,
        )

        valid, suppressed = validate_and_filter_drafts_for_context(
            [draft_valid, draft_unrelated], ctx_batch
        )
        assert len(valid) == 1
        assert valid[0].file == "app/auth.py"
        assert len(suppressed) == 1
        assert suppressed[0]["reason"] == "INVALID_FILE"

    def test_global_push_capping(self):
        findings = [
            _make_scout_draft(title="F1", file="app/a.py", confidence=0.8, impact="meaningful"),
            _make_scout_draft(title="F2", file="app/a.py", confidence=0.9, impact="meaningful"),
            _make_scout_draft(title="F3", file="app/b.py", confidence=0.7, impact="meaningful"),
            _make_scout_draft(title="F4", file="app/c.py", confidence=0.85, impact="meaningful"),
            _make_scout_draft(title="F5", file="app/d.py", confidence=0.95, impact="major"),
        ]
        # Rules: max 1 per file, max 3 per push
        capped, logs = rank_and_cap_push_findings(findings, max_escalated=3, max_per_file=1)
        assert len(capped) == 3
        # Should pick:
        # app/d.py (major, 0.95)
        # app/a.py (meaningful, 0.9 - F2 preferred over F1)
        # app/c.py (meaningful, 0.85 - F4 preferred over F3)
        files = [f.file for f in capped]
        assert files == ["app/d.py", "app/a.py", "app/c.py"]
        assert len(set(files)) == 3


class TestScoutAgentGuard:
    @pytest.mark.asyncio
    async def test_scout_agent_guard_blocks_oversized_envelope(self):
        mock_provider = AsyncMock()
        agent = ScoutAgent(llm_provider=mock_provider)

        event = _make_push_event()
        ctx = ScoutContext(
            installation_id=1,
            repository_id=123,
            owner="test-owner",
            name="test-repo",
            before_sha="0" * 40,
            commit_sha="1" * 40,
            changed_paths=["app/main.py"],
        )

        with patch("app.scout.agent.estimate_serialized_request_size", return_value=97_000):
            with pytest.raises(MalformedScoutResponse, match="exceeds internal safety budget"):
                await agent.discover(event, ctx)
        mock_provider.complete.assert_not_called()


class TestGroqProvider413Handling:
    @pytest.mark.asyncio
    async def test_groq_provider_sanitizes_413(self):
        provider = GroqProvider(api_key="test-key", model="qwen/qwen3.8-27b")
        req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        resp = httpx.Response(status_code=413, request=req, text="Payload Too Large")

        with patch("httpx.AsyncClient.post", return_value=resp):
            with pytest.raises(LLMInvocationError) as exc_info:
                await provider.complete("system", "user")

            err_msg = str(exc_info.value)
            assert "HTTP 413" in err_msg
            assert "test-key" not in err_msg
            assert "Authorization" not in err_msg


class TestAdversarialScoutScenarios:
    @pytest.mark.asyncio
    async def test_adversarial_20_files_with_huge_readme_and_unrelated_tests(self):
        # 1. Setup 20 changed files
        changed_files = [
            ChangedFile(
                path=f"app/core/service_{i}.py",
                patch="+" + ("line_of_code();\n" * 150),  # ~2.5KB patch
            )
            for i in range(20)
        ]

        # 2. Partition
        batches = partition_changed_files_for_scout(
            changed_files,
            max_files_per_batch=5,
            max_estimated_bytes_per_batch=35_000,
        )
        assert len(batches) >= 4

        # Verify all 20 assigned
        flat = [f.path for b in batches for f in b]
        assert len(flat) == 20
        assert len(set(flat)) == 20

        # 3. Mock client with huge README, 50 issues, and unrelated tests
        mock_client = AsyncMock()
        mock_client.get_file_content.side_effect = lambda owner, repo, path, ref: (
            "# Giant Readme\n" + ("Information\n" * 3000)
            if path.lower() == "readme.md"
            else "def run():\n    pass\n" * 500  # ~8KB file
        )
        tree_items = [
            {"path": "README.md", "type": "blob"},
            {"path": "tests/test_unrelated_1.py", "type": "blob"},
            {"path": "tests/test_unrelated_2.py", "type": "blob"},
            {"path": "tests/test_unrelated_3.py", "type": "blob"},
            {"path": "tests/core/test_service_0.py", "type": "blob"}, # Relevant Tier 2
        ]
        mock_client.get_repository_tree.return_value = (tree_items, False)
        mock_client.list_recent_closed_issues.return_value = [
            ScoutExistingIssue(number=i, title=f"Issue #{i}", body_summary="Fixed a bug")
            for i in range(50)
        ]

        builder = ScoutContextBuilder(client=mock_client)
        event = _make_push_event()

        all_batch_survivors = []

        # 4. Process each batch independently
        for batch_idx, batch in enumerate(batches, start=1):
            ctx = await builder.build_context(event, batch)
            
            # Layer 1 budget checks
            assert ctx.total_context_bytes <= MAX_TOTAL_CONTEXT_BYTES
            assert len(ctx.readme.encode("utf-8")) <= MAX_README_BYTES
            assert len(ctx.existing_issues) <= 50

            # Test relevance check: unrelated tests excluded
            test_paths = [t.path for t in ctx.test_files]
            assert "tests/test_unrelated_1.py" not in test_paths
            assert "tests/test_unrelated_2.py" not in test_paths
            assert "tests/test_unrelated_3.py" not in test_paths

            # Layer 2 prompt check
            prompt = build_scout_prompt(event, ctx, max_request_bytes=MAX_SCOUT_REQUEST_BYTES)
            req_size = estimate_serialized_request_size(prompt, SYSTEM_PROMPT)
            assert req_size <= MAX_SCOUT_REQUEST_BYTES

            # Simulate Scout draft for first file in this batch
            first_file = batch[0].path
            simulated_draft = _make_scout_draft(
                title=f"Bug in {first_file}",
                file=first_file,
                snippet="def run():",
                confidence=min(0.95, 0.80 + (batch_idx * 0.02)),
            )
            # Simulate a hallucinated cross-batch draft (referencing a file from batch 20)
            hallucinated_draft = _make_scout_draft(
                title="Cross batch hallucination",
                file="app/core/service_19.py",
                snippet="def run():",
                confidence=0.99,
            )

            survivors, _ = validate_and_filter_drafts_for_context(
                [simulated_draft, hallucinated_draft], ctx
            )
            # Batch must reject service_19.py if it is not in this batch
            if "app/core/service_19.py" not in [f.path for f in batch]:
                assert all(s.file != "app/core/service_19.py" for s in survivors)

            all_batch_survivors.extend(survivors)

        # 5. Global push-wide ranking and capping
        final_findings, _ = rank_and_cap_push_findings(
            all_batch_survivors, max_escalated=3, max_per_file=1
        )
        assert len(final_findings) <= 3
        final_files = [f.file for f in final_findings]
        assert len(final_files) == len(set(final_files))

    @pytest.mark.asyncio
    async def test_single_pathological_file_bounded(self):
        # 1 pathological file with 100KB source and 50KB patch
        cf = ChangedFile(
            path="app/pathological.py",
            patch="+" + ("patch_line\n" * 4000),  # ~44KB patch
        )
        mock_client = AsyncMock()
        mock_client.get_file_content.return_value = "source_line\n" * 9000  # ~100KB source
        mock_client.get_repository_tree.return_value = ([], False)
        mock_client.list_recent_closed_issues.return_value = []

        builder = ScoutContextBuilder(client=mock_client)
        event = _make_push_event()

        ctx = await builder.build_context(event, [cf])
        assert len(ctx.files) == 1
        f = ctx.files[0]
        assert len(f.content.encode("utf-8")) <= MAX_FILE_BYTES
        assert len(f.patch.encode("utf-8")) <= MAX_PATCH_BYTES
        assert ctx.context_completeness == ContextCompleteness.PARTIAL
        assert "FILE_TRUNCATED" in ctx.partial_reasons
        assert "PATCH_TRUNCATED" in ctx.partial_reasons

        # Layer 2 check
        prompt = build_scout_prompt(event, ctx, max_request_bytes=MAX_SCOUT_REQUEST_BYTES)
        req_size = estimate_serialized_request_size(prompt, SYSTEM_PROMPT)
        assert req_size <= MAX_SCOUT_REQUEST_BYTES

