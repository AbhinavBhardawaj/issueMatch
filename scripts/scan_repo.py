"""Direct Repository Bug Scanner (Scout + Verifier + Issue Creation).

Scans the existing codebase of a repository at current HEAD, runs Issue Scout (Finder)
to discover genuine bugs, passes them to the Independent Verifier Pipeline, and
authorizes Issue Creation on GitHub.

Usage:
    python scripts/scan_repo.py
    python scripts/scan_repo.py --owner koushiksuresh27 --repo NitiFlow
"""
import os
import sys
import asyncio
import argparse
import logging
from dotenv import load_dotenv

# Ensure project root in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
load_dotenv()

# Default to NVIDIA NIM if not explicitly set
os.environ.setdefault("SCOUT_PROVIDER", "NVIDIA")
os.environ.setdefault("SCOUT_MODEL_ID", "nvidia/nemotron-3-super-120b-a12b")
os.environ.setdefault("SCOUT_TIMEOUT", "300.0")
os.environ.setdefault("VERIFIER_PROVIDER", "NVIDIA")
os.environ.setdefault("VERIFIER_MODEL_ID", "nvidia/nemotron-3-super-120b-a12b")
os.environ.setdefault("VERIFIER_TIMEOUT", "300.0")
os.environ.setdefault("LLM_TIMEOUT_SECONDS", "300.0")

from app.github.app_auth import GitHubAppConfig, GitHubAppAuthenticator
from app.github.events import GitHubPushEvent
from app.services.issue_gate import SQLiteDedupStore
from app.services.write_journal import SQLiteIssueWriteJournal
from app.main import build_scout_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("scan_repo")


async def main():
    parser = argparse.ArgumentParser(description="Scan current repository codebase for bugs.")
    parser.add_argument("--owner", default=os.getenv("GITHUB_REPO_OWNER", "koushiksuresh27"), help="Repository owner")
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPO_NAME", "NitiFlow"), help="Repository name")
    parser.add_argument("--installation-id", type=int, default=int(os.getenv("GITHUB_INSTALLATION_ID", "162794652")), help="GitHub App installation ID")
    parser.add_argument("--db-path", default=os.getenv("ISSUE_ANALYZER_DB_PATH", "issuematch-live.db"), help="Path to SQLite DB")
    parser.add_argument("--all", action="store_true", help="Scan entire repository tree instead of latest commit")
    args = parser.parse_args()

    print(f"\n========================================================")
    print(f" Issue Scout + Verifier: Scanning Codebase")
    print(f" Target: https://github.com/{args.owner}/{args.repo}")
    print(f"========================================================\n")

    # 1. Authenticate with GitHub App
    logger.info("Authenticating with GitHub App...")
    config = GitHubAppConfig.from_environment()
    authenticator = GitHubAppAuthenticator(config)
    client = await authenticator.create_installation_client(args.installation_id)

    # 2. Get repository details and latest HEAD commit
    logger.info(f"Fetching repository metadata for {args.owner}/{args.repo}...")
    repo_data = await client.get_repository(args.owner, args.repo)
    repo_id = int(repo_data["id"])
    default_branch = repo_data.get("default_branch", "main")

    commit_data = await client._request("GET", f"/repos/{args.owner}/{args.repo}/commits/{default_branch}")
    head_sha = commit_data["sha"]
    
    if args.all:
        before_sha = "0" * 40
        logger.info(f"Full-tree scan mode enabled at commit {head_sha[:10]}")
    else:
        parents = commit_data.get("parents", [])
        before_sha = parents[0]["sha"] if parents else "0" * 40
        commit_msg = commit_data.get("commit", {}).get("message", "").splitlines()[0]
        logger.info(f"Scanning latest commit: '{commit_msg}' ({head_sha[:10]}) against parent ({before_sha[:10]})")

    # 3. Setup persistent stores and ScoutService
    dedup_store = SQLiteDedupStore(args.db_path)
    write_journal = SQLiteIssueWriteJournal(args.db_path)

    async def client_factory(inst_id: int):
        return await authenticator.create_installation_client(inst_id)

    logger.info("Initializing Scout Agent and Verifier Pipeline...")
    scout_service = build_scout_service(
        dedup_store=dedup_store,
        write_journal=write_journal,
        client_factory=client_factory,
    )

    # 4. Formulate scan event
    event = GitHubPushEvent(
        delivery_id=f"manual-scan-{head_sha[:8]}",
        repository_owner=args.owner,
        repository_name=args.repo,
        repository_id=repo_id,
        installation_id=args.installation_id,
        default_branch=default_branch,
        ref=f"refs/heads/{default_branch}",
        before_sha=before_sha,
        after_sha=head_sha,
        forced=False,
        deleted=False,
    )

    logger.info("Running Scout Agent (LLM code analysis)...")
    result = await scout_service.process_push(event)

    print(f"\n========================================================")
    print(f" Scan Complete Summary")
    print(f"========================================================")
    print(f" Files inspected:        {len(result.changed_files)}")
    print(f" AI Drafts discovered:   {result.ai_drafts}")
    print(f" Suppressed/Filtered:    {result.suppressed_findings}")
    print(f" Escalated to Verifier:  {result.escalated_findings}")
    print(f" Verifier Rejected:      {result.verifier_rejected}")
    print(f" Issues Created on GitHub: {result.issues_created}")
    if result.failures:
        print(f" Warnings/Failures:      {result.failures}")
    print(f"========================================================\n")


if __name__ == "__main__":
    asyncio.run(main())
