"""Safe Developer Preflight Diagnostic Tool for Live Scout & Verifier.

Tests mandatory prerequisites for live end-to-end sandbox testing:
1. GitHub App configuration loads (GITHUB_APP_ID, GITHUB_PRIVATE_KEY)
2. Installation token can be obtained for LIVE_E2E_INSTALLATION_ID
3. Sandbox repository can be read (LIVE_E2E_OWNER / LIVE_E2E_REPO)
4. Repository numeric ID matches LIVE_E2E_EXPECTED_REPO_ID
5. Target commit SHA is accessible in repository (LIVE_E2E_COMMIT_SHA)
6. Parent commit SHA is accessible in repository (LIVE_E2E_BEFORE_SHA)
7. Scout LLM provider initializes
8. Verifier LLM provider initializes
9. SQLite database path is writable
10. Live issue writes status (LIVE_E2E_ALLOW_ISSUE_WRITES == 1 or 0)

CRITICAL SAFETY INVARIANT:
This script performs ZERO GitHub issue POSTs or mutations.
Never logs API keys, private keys, or tokens.
Exits 0 on success, 1 on any failed mandatory prerequisite.
"""
import os
import sys
import asyncio
import sqlite3
from pathlib import Path
from dotenv import load_dotenv

# Explicitly resolve repository root .env; OS environment variables still take precedence (override=False)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ENV = PROJECT_ROOT / ".env"
if PROJECT_ENV.is_file():
    load_dotenv(dotenv_path=PROJECT_ENV, override=False)
else:
    load_dotenv(override=False)

# Ensure project root is in sys.path
sys.path.insert(0, str(PROJECT_ROOT))

from app.storage.sqlite import get_default_db_path
from app.github.app_auth import GitHubAppConfig, GitHubAppAuthenticator, GitHubConfigurationError
from app.infrastructure.llm_router import (
    create_scout_provider,
    create_verifier_provider,
    MultiLLMProvider,
    ProviderConfigurationError,
)
from app.verifier.schemas import LLMInvocationError


async def run_diagnostics() -> bool:
    print("=" * 60)
    print("LIVE SCOUT & VERIFIER PREFLIGHT DIAGNOSTICS")
    print("=" * 60)

    all_passed = True

    def check(name: str, passed: bool, detail: str):
        nonlocal all_passed
        status_str = "[PASS]" if passed else "[FAIL]"
        print(f"  {status_str} {name}: {detail}")
        if not passed:
            all_passed = False

    # 0. Root Environment Configuration
    if PROJECT_ENV.is_file():
        check("Root environment configuration loaded", True, f"{PROJECT_ENV.name}")
    else:
        check("Root environment configuration loaded", True, "from system environment")

    run_live = os.environ.get("RUN_LIVE_SCOUT_E2E", "").strip()
    if run_live == "1":
        check("RUN_LIVE_SCOUT_E2E enabled", True, "RUN_LIVE_SCOUT_E2E=1")
    else:
        check("RUN_LIVE_SCOUT_E2E enabled", False, "RUN_LIVE_SCOUT_E2E != 1 (live tests will skip)")

    # 1. GitHub App Credentials
    authenticator = None
    try:
        app_config = GitHubAppConfig.from_environment()
        authenticator = GitHubAppAuthenticator(app_config)
        check("GitHub App configuration constructed", True, f"App ID {app_config.app_id}")
    except GitHubConfigurationError as exc:
        check("GitHub App configuration constructed", False, f"Failed to load: {exc}")
    except Exception as exc:
        check("GitHub App configuration constructed", False, f"Failed to load: {exc}")

    # 2. Installation Client & Token
    inst_id_str = os.environ.get("LIVE_E2E_INSTALLATION_ID", "").strip()
    client = None
    if not inst_id_str:
        check("Installation Token", False, "LIVE_E2E_INSTALLATION_ID not configured")
    elif not authenticator:
        check("Installation Token", False, "Skipped (GitHub App config invalid)")
    else:
        try:
            inst_id = int(inst_id_str)
            client = await authenticator.create_installation_client(inst_id)
            check("Installation Token", True, f"Obtained for installation {inst_id}")
        except Exception as exc:
            check("Installation Token", False, f"Token exchange failed: {exc}")

    # 3. Sandbox Repository Read & Numeric ID Match
    owner = os.environ.get("LIVE_E2E_OWNER", "").strip()
    repo = os.environ.get("LIVE_E2E_REPO", "").strip()
    expected_repo_id_str = os.environ.get("LIVE_E2E_EXPECTED_REPO_ID", "").strip()

    if not owner or not repo:
        check("Sandbox Repository", False, "LIVE_E2E_OWNER or LIVE_E2E_REPO not configured")
    elif not client:
        check("Sandbox Repository", False, "Skipped (no GitHub client)")
    else:
        try:
            repo_data = await client.get_repository(owner, repo)
            actual_repo_id = repo_data.get("id")
            check("Sandbox Repository Read", True, f"Found '{owner}/{repo}' (default branch: {repo_data.get('default_branch')})")

            if expected_repo_id_str:
                expected_id = int(expected_repo_id_str)
                if actual_repo_id == expected_id:
                    check("Repository ID Match", True, f"Matches expected {expected_id}")
                else:
                    check("Repository ID Match", False, f"Mismatch: expected {expected_id}, actual {actual_repo_id}")
            else:
                check("Repository ID Match", False, "LIVE_E2E_EXPECTED_REPO_ID not configured")
        except Exception as exc:
            check("Sandbox Repository Read", False, f"Failed to fetch repo: {exc}")

    # 4. Commit SHAs Accessibility
    commit_sha = os.environ.get("LIVE_E2E_COMMIT_SHA", "").strip()
    before_sha = os.environ.get("LIVE_E2E_BEFORE_SHA", "").strip()

    if not commit_sha:
        check("Target Commit SHA", False, "LIVE_E2E_COMMIT_SHA not configured")
    elif not client or not owner or not repo:
        check("Target Commit SHA", False, "Skipped (no repository client)")
    else:
        try:
            tree_items, _ = await client.get_repository_tree(owner, repo, ref=commit_sha)
            check("Target Commit SHA", True, f"SHA {commit_sha[:10]}... accessible ({len(tree_items)} tree items)")
        except Exception as exc:
            check("Target Commit SHA", False, f"Failed to read tree at commit {commit_sha[:10]}...: {exc}")

    if before_sha and client and owner and repo:
        try:
            before_tree, _ = await client.get_repository_tree(owner, repo, ref=before_sha)
            check("Parent Commit SHA", True, f"SHA {before_sha[:10]}... accessible ({len(before_tree)} tree items)")
        except Exception as exc:
            check("Parent Commit SHA", False, f"Failed to read tree at commit {before_sha[:10]}...: {exc}")
    elif not before_sha:
        check("Parent Commit SHA", False, "LIVE_E2E_BEFORE_SHA not configured")

    # 5. Scout LLM Provider
    try:
        scout_prov = create_scout_provider()
        if isinstance(scout_prov, MultiLLMProvider) and len(scout_prov.providers) == 0:
            check("Scout provider configuration constructed", False, "No active providers loaded")
        else:
            check("Scout provider configuration constructed", True, f"{type(scout_prov).__name__}")
    except (ProviderConfigurationError, LLMInvocationError, Exception) as exc:
        check("Scout provider configuration constructed", False, f"Initialization failed: {exc}")

    # 6. Verifier LLM Provider
    try:
        verifier_prov = create_verifier_provider()
        if isinstance(verifier_prov, MultiLLMProvider) and len(verifier_prov.providers) == 0:
            check("Verifier provider configuration constructed", False, "No active providers loaded")
        else:
            check("Verifier provider configuration constructed", True, f"{type(verifier_prov).__name__}")
    except (ProviderConfigurationError, LLMInvocationError, Exception) as exc:
        check("Verifier provider configuration constructed", False, f"Initialization failed: {exc}")

    # 7. SQLite Writability
    db_path = get_default_db_path()
    try:
        db_dir = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(db_dir, exist_ok=True)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS _preflight_diag (id INT)")
            conn.execute("DROP TABLE _preflight_diag")
            conn.commit()
        finally:
            conn.close()
        check("Scout/Verifier SQLite writable", True, f"'{db_path}'")
    except Exception as exc:
        check("Scout/Verifier SQLite writable", False, f"Failed to write DB at '{db_path}': {exc}")

    # 8. Issue Writes Policy Check
    allow_writes = os.environ.get("LIVE_E2E_ALLOW_ISSUE_WRITES") == "1"
    if allow_writes:
        print("  [WARN] Issue Writes Status: ENABLED (LIVE_E2E_ALLOW_ISSUE_WRITES=1) -- Issues may be created")
    else:
        print("  [INFO] Issue Writes Status: DISABLED (LIVE_E2E_ALLOW_ISSUE_WRITES != 1) -- Read-only safe mode")

    print("=" * 60)
    if all_passed:
        print("PREFLIGHT STATUS: READY FOR LIVE E2E TESTING")
    else:
        print("PREFLIGHT STATUS: BLOCKED (Resolve failed items before live testing)")
    print("=" * 60)
    return all_passed


if __name__ == "__main__":
    passed = asyncio.run(run_diagnostics())
    sys.exit(0 if passed else 1)
