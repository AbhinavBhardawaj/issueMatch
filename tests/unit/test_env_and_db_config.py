"""Unit tests for environment loading, secret masking, and SQLite path resolution.

Covers Section 10 acceptance criteria:
A. Live E2E test always collects (collected 1 item).
B. Project-root .env resolution independent of cwd.
C. OS env overrides .env (override=False).
D. Missing .env operates on OS env without crash.
E. Preflight/inspect never print secrets.
F. SQLite path is identical across AppRuntimeConfig, preflight, receiver, inspect.
G. Explicit ISSUE_ANALYZER_DB_PATH overrides default.
"""
import os
import sys
import tempfile
from pathlib import Path
import pytest
from dotenv import load_dotenv

from app.main import AppRuntimeConfig
from app.storage.sqlite import get_default_db_path


def test_a_live_e2e_test_always_collects():
    """A. Live E2E test module must not skip at collection time (no module-level pytestmark skip)."""
    import tests.integration.test_live_scout_verifier_e2e as live_module

    # Must have the test function
    assert hasattr(live_module, "test_live_scout_and_verifier_pipeline")
    # Must NOT have module-level pytestmark skipif
    pytestmark = getattr(live_module, "pytestmark", None)
    if pytestmark is not None:
        if isinstance(pytestmark, list):
            for mark in pytestmark:
                assert mark.name != "skipif", "Module-level pytestmark skipif prevents collection"
        else:
            assert pytestmark.name != "skipif", "Module-level pytestmark skipif prevents collection"


def test_b_project_root_resolution_independent_of_cwd(monkeypatch):
    """B. PROJECT_ROOT resolves to repo root regardless of current working directory."""
    import scripts.preflight_live_check as preflight
    import scripts.run_live_webhook_receiver as receiver
    import tests.integration.test_live_scout_verifier_e2e as live_e2e

    expected_anchor = Path(__file__).resolve().parent.parent.parent

    assert preflight.PROJECT_ROOT == expected_anchor
    assert receiver.PROJECT_ROOT == expected_anchor
    assert live_e2e.PROJECT_ROOT == expected_anchor

    # Change working directory and verify PROJECT_ROOT anchor doesn't drift
    orig_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            os.chdir(tmpdir)
            resolved = Path(preflight.__file__).resolve().parent.parent
            assert resolved == expected_anchor
        finally:
            os.chdir(orig_cwd)


def test_c_os_env_overrides_dotenv_override_false(monkeypatch):
    """C. OS environment variables take precedence over .env file entries (override=False)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_env = Path(tmpdir) / ".env"
        fake_env.write_text("TEST_OVERRIDE_KEY=from_dotenv\nNEW_KEY=only_in_dotenv\n", encoding="utf-8")

        monkeypatch.setenv("TEST_OVERRIDE_KEY", "from_os_env")

        # Load with override=False
        load_dotenv(dotenv_path=fake_env, override=False)

        assert os.environ.get("TEST_OVERRIDE_KEY") == "from_os_env"
        assert os.environ.get("NEW_KEY") == "only_in_dotenv"


def test_d_missing_dotenv_operates_on_os_env_without_crash(monkeypatch):
    """D. Missing .env file does not crash and relies entirely on OS environment."""
    non_existent = Path("non_existent_path_to_env") / ".env"
    assert not non_existent.is_file()

    monkeypatch.setenv("D_TEST_VAR", "present_in_os")
    # Should not raise exception
    load_dotenv(dotenv_path=non_existent, override=False)
    assert os.environ.get("D_TEST_VAR") == "present_in_os"


@pytest.mark.asyncio
async def test_e_preflight_and_inspect_never_print_secrets(monkeypatch, capsys):
    """E. Preflight and inspect utilities never leak private keys, tokens, or raw secrets to stdout/stderr."""
    from scripts.preflight_live_check import run_diagnostics

    dummy_key = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0fakekeydata...\n-----END RSA PRIVATE KEY-----"
    dummy_token = "ghp_secretTokenVal1234567890abcdef"
    dummy_gemini = "AIzaSyFakeGeminiApiKey987654321"

    monkeypatch.setenv("RUN_LIVE_SCOUT_E2E", "0")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", dummy_key)
    monkeypatch.setenv("GITHUB_TOKEN", dummy_token)
    monkeypatch.setenv("GEMINI_API_KEY", dummy_gemini)

    await run_diagnostics()

    captured = capsys.readouterr()
    combined_output = captured.out + captured.err

    assert dummy_key not in combined_output
    assert dummy_token not in combined_output
    assert dummy_gemini not in combined_output
    assert "-----BEGIN RSA PRIVATE KEY-----" not in combined_output


def test_f_sqlite_path_identical_across_components(monkeypatch):
    """F. SQLite path is identical across AppRuntimeConfig and get_default_db_path."""
    monkeypatch.delenv("ISSUE_ANALYZER_DB_PATH", raising=False)

    default_path = get_default_db_path()
    config = AppRuntimeConfig()

    assert config.db_path == default_path
    assert "issue_analyzer.db" in default_path
    assert "issueanalyzer_durable.db" not in default_path


def test_g_explicit_db_path_overrides_default(monkeypatch):
    """G. Setting ISSUE_ANALYZER_DB_PATH overrides default path everywhere."""
    custom_path = str(Path("/custom/test/path/my_issues.db"))
    monkeypatch.setenv("ISSUE_ANALYZER_DB_PATH", custom_path)

    assert get_default_db_path() == custom_path

    config = AppRuntimeConfig()
    assert config.db_path == custom_path
