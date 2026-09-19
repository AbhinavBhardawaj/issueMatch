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
