import pytest
from app.github.scout_repository import ScoutGitHubReadClient
import inspect
import ast
from pathlib import Path


def test_scout_github_read_client_has_no_write_methods():
    read_methods = [
        m for m in dir(ScoutGitHubReadClient) if not m.startswith("_")
    ]
    forbidden = ["create_issue", "create_issue_comment", "update_issue", "close_issue"]
    for method in forbidden:
        assert method not in read_methods


def test_scout_agent_imports_no_write_or_verifier_pipeline():
    agent_path = Path("app/scout/agent.py")
    tree = ast.parse(agent_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "issue_creator" not in alias.name
                assert "pipeline" not in alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                assert "issue_creator" not in node.module
                assert "pipeline" not in node.module


def test_no_forbidden_execution_in_scout_code():
    scout_dir = Path("app/scout")
    forbidden_calls = ["eval(", "exec(", "subprocess", "os.system", "shell=True"]
    for py_file in scout_dir.glob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        for bad in forbidden_calls:
            assert bad not in content, f"Forbidden call {bad} found in {py_file}"


def test_scout_agent_and_verifier_agent_capability_separation():
    from app.scout.agent import ScoutAgent
    from app.verifier.agent import run_verifier

    # ScoutAgent AST analysis
    tree_scout = ast.parse(Path("app/scout/agent.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree_scout):
        if isinstance(node, ast.Name):
            assert node.id not in ("create_issue", "IssueCreator", "VerifierPipeline", "write_client")
        elif isinstance(node, ast.Attribute):
            assert node.attr not in ("create_issue", "write_client")

    # VerifierAgent AST analysis
    tree_verifier = ast.parse(Path("app/verifier/agent.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree_verifier):
        if isinstance(node, ast.Name):
            assert node.id not in ("create_issue", "IssueCreator", "write_client")
        elif isinstance(node, ast.Attribute):
            assert node.attr not in ("create_issue", "write_client")

    # Dynamic check: ScoutAgent instance has no write methods
    mock_llm = type("MockLLM", (), {"complete": lambda *args: None})()
    agent = ScoutAgent(llm_provider=mock_llm)
    agent_methods = dir(agent)
    for forbidden in ["create_issue", "github_client", "write_client", "verifier_pipeline"]:
        assert forbidden not in agent_methods


def test_only_issue_creator_calls_github_create_issue():
    # Only app/services/issue_creator.py should call create_issue on client
    app_dir = Path("app")
    for py_file in app_dir.rglob("*.py"):
        if py_file.name in ("issue_creator.py", "client.py", "pipeline.py", "service.py"):
            continue
        code = py_file.read_text(encoding="utf-8")
        assert ".create_issue(" not in code, f"Unexpected create_issue call in {py_file}"
