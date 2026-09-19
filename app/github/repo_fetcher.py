import asyncio
from enum import Enum
from pathlib import Path
from app.domain.models import (
    Finding,
    RepoContext,
    RepoContextFile,
    ExistingIssue,
    ContextCompleteness,
)
from app.github.client import (
    GitHubClient,
    GitHubNotFoundError,
    GitHubRequestTimeoutError,
    GitHubRateLimitError,
    GitHubServerError,
    GitHubClientError,
)
from app.scout.context import (
    _is_supported_path,
    _is_test_file,
    _extract_imports,
    truncate_utf8_bytes,
)

MAX_VERIFIER_FILE_BYTES = 20_000
MAX_VERIFIER_TOTAL_BYTES = 120_000
MAX_VERIFIER_CALLER_FILES = 5
MAX_VERIFIER_TEST_FILES = 3


class FetchOutcome(str, Enum):
    FOUND = "FOUND"
    ABSENT = "ABSENT"
    INCOMPLETE = "INCOMPLETE"


async def _fetch_single_file(
    client: GitHubClient, owner: str, repo: str, path: str, ref: str
) -> tuple[FetchOutcome, str | None, str | None]:
    """
    Fetch a single file from GitHub pinned to ref.
    Distinguishes 404 (ABSENT) from transient network/API failures (INCOMPLETE).
    """
    try:
        content = await client.get_file_content(owner, repo, path, ref=ref)
        return FetchOutcome.FOUND, content, None
    except GitHubNotFoundError:
        return FetchOutcome.ABSENT, None, None
    except GitHubRequestTimeoutError:
        return FetchOutcome.INCOMPLETE, None, "TIMEOUT"
    except GitHubRateLimitError:
        return FetchOutcome.INCOMPLETE, None, "429_RATELIMIT"
    except GitHubServerError:
        return FetchOutcome.INCOMPLETE, None, "500_SERVER_ERROR"
    except Exception as exc:
        return FetchOutcome.INCOMPLETE, None, f"FETCH_FAILED:{type(exc).__name__}"


async def fetch_repo_context(
    finding: Finding,
    client: GitHubClient,
    owner: str,
    repo_name: str,
) -> RepoContext:
    """
    Build a bounded, targeted, independent Verifier context pinned to finding.commit_sha.
    Searches for counter-evidence, callers, related tests, documentation, and existing issues.
    Tracks context completeness and omissions deterministically.
    """
    partial_reasons: list[str] = []
    omissions: list[str] = []
    completeness = ContextCompleteness.COMPLETE
    total_bytes = 0

    seen_paths = set()
    primary_and_evidence_paths = [finding.file]
    for ev in finding.evidence:
        if ev.file not in primary_and_evidence_paths:
            primary_and_evidence_paths.append(ev.file)

    files: list[RepoContextFile] = []
    caller_files: list[RepoContextFile] = []
    test_files: list[RepoContextFile] = []

    # 1. Fetch Primary Finding File and Evidence Files
    primary_content = None
    for path in primary_and_evidence_paths:
        seen_paths.add(path)
        outcome, raw_content, error_tag = await _fetch_single_file(
            client, owner, repo_name, path, ref=finding.commit_sha
        )
        if outcome == FetchOutcome.FOUND and raw_content is not None:
            safe_content, truncated = truncate_utf8_bytes(raw_content, MAX_VERIFIER_FILE_BYTES)
            if truncated:
                completeness = ContextCompleteness.PARTIAL
                partial_reasons.append(f"FILE_TRUNCATED:{path}")
            scout_f = RepoContextFile(
                path=path,
                content=safe_content,
                size_bytes=len(safe_content.encode("utf-8")),
            )
            files.append(scout_f)
            total_bytes += scout_f.size_bytes
            if path == finding.file:
                primary_content = safe_content
        elif outcome == FetchOutcome.INCOMPLETE:
            completeness = ContextCompleteness.PARTIAL
            partial_reasons.append(f"TRANSIENT_FAILURE:{path}:{error_tag}")
            omissions.append(f"Could not inspect {path} due to {error_tag}")
        elif outcome == FetchOutcome.ABSENT:
            # 404 is authoritative absence at this ref
            pass

    # 2. Inspect Repository Tree for Callers and Tests (Pinned to commit_sha)
    tree_items: list[dict] = []
    try:
        raw_tree, tree_truncated = await client.get_repository_tree(
            owner, repo_name, ref=finding.commit_sha
        )
        tree_items = raw_tree
        if tree_truncated:
            completeness = ContextCompleteness.PARTIAL
            partial_reasons.append("TREE_TRUNCATED")
    except GitHubRequestTimeoutError:
        completeness = ContextCompleteness.PARTIAL
        partial_reasons.append("TRANSIENT_FAILURE:TREE:TIMEOUT")
        omissions.append("Repository tree lookup timed out")
    except GitHubRateLimitError:
        completeness = ContextCompleteness.PARTIAL
        partial_reasons.append("TRANSIENT_FAILURE:TREE:429_RATELIMIT")
        omissions.append("Repository tree lookup rate-limited")
    except GitHubServerError:
        completeness = ContextCompleteness.PARTIAL
        partial_reasons.append("TRANSIENT_FAILURE:TREE:500_SERVER_ERROR")
        omissions.append("Repository tree lookup failed on server error")
    except Exception as exc:
        completeness = ContextCompleteness.PARTIAL
        partial_reasons.append(f"TREE_LOOKUP_FAILED:{type(exc).__name__}")

    all_tree_paths = [item["path"] for item in tree_items if item.get("type") == "blob" and "path" in item]

    # 3. Direct Static Imports of Primary File
    if primary_content:
        direct_imports = _extract_imports(finding.file, primary_content)
        all_import_candidates = [
            p for p in all_tree_paths
            if p not in seen_paths and any(p.endswith(rel) for rel in direct_imports) and _is_supported_path(p)
        ]
        if len(all_import_candidates) > 3:
            completeness = ContextCompleteness.PARTIAL
            partial_reasons.append("IMPORT_CANDIDATE_LIMIT_REACHED")
        import_candidates = all_import_candidates[:3]

        for imp_path in import_candidates:
            if total_bytes >= MAX_VERIFIER_TOTAL_BYTES:
                completeness = ContextCompleteness.PARTIAL
                partial_reasons.append("CONTEXT_BUDGET_REACHED")
                break
            seen_paths.add(imp_path)
            outcome, content, error_tag = await _fetch_single_file(
                client, owner, repo_name, imp_path, ref=finding.commit_sha
            )
            if outcome == FetchOutcome.FOUND and content is not None:
                safe_c, _ = truncate_utf8_bytes(content, MAX_VERIFIER_FILE_BYTES)
                rc_f = RepoContextFile(path=imp_path, content=safe_c, size_bytes=len(safe_c.encode("utf-8")))
                files.append(rc_f)
                total_bytes += rc_f.size_bytes
            elif outcome == FetchOutcome.INCOMPLETE:
                completeness = ContextCompleteness.PARTIAL
                partial_reasons.append(f"TRANSIENT_FAILURE:{imp_path}:{error_tag}")
                omissions.append(f"Could not inspect import {imp_path} due to {error_tag}")

    # 4. Search for Callers / References (if finding.function specified)
    if finding.function:
        target_fn = finding.function.strip()
        all_caller_candidates = [
            p for p in all_tree_paths
            if p not in seen_paths and not _is_test_file(p) and _is_supported_path(p)
        ]
        MAX_CALLER_SCAN_CANDIDATES = 10
        if len(all_caller_candidates) > MAX_CALLER_SCAN_CANDIDATES:
            completeness = ContextCompleteness.PARTIAL
            partial_reasons.append("CALLER_SCAN_LIMIT_REACHED")
        candidate_caller_paths = all_caller_candidates[:MAX_CALLER_SCAN_CANDIDATES]

        for cand_path in candidate_caller_paths:
            if total_bytes >= MAX_VERIFIER_TOTAL_BYTES:
                completeness = ContextCompleteness.PARTIAL
                partial_reasons.append("CONTEXT_BUDGET_REACHED")
                break
            if len(caller_files) >= MAX_VERIFIER_CALLER_FILES:
                completeness = ContextCompleteness.PARTIAL
                partial_reasons.append("CALLER_FILE_LIMIT_REACHED")
                break
            seen_paths.add(cand_path)
            outcome, content, error_tag = await _fetch_single_file(
                client, owner, repo_name, cand_path, ref=finding.commit_sha
            )
            if outcome == FetchOutcome.FOUND and content is not None:
                if target_fn in content:
                    safe_c, _ = truncate_utf8_bytes(content, MAX_VERIFIER_FILE_BYTES)
                    rc_f = RepoContextFile(path=cand_path, content=safe_c, size_bytes=len(safe_c.encode("utf-8")))
                    caller_files.append(rc_f)
                    total_bytes += rc_f.size_bytes
            elif outcome == FetchOutcome.INCOMPLETE:
                completeness = ContextCompleteness.PARTIAL
                partial_reasons.append(f"TRANSIENT_FAILURE:{cand_path}:{error_tag}")
                omissions.append(f"Could not inspect caller candidate {cand_path} due to {error_tag}")

    # 5. Nearby / Relevant Test Files
    all_test_candidates = [
        p for p in all_tree_paths
        if p not in seen_paths and _is_test_file(p) and _is_supported_path(p)
    ]
    if len(all_test_candidates) > MAX_VERIFIER_TEST_FILES:
        completeness = ContextCompleteness.PARTIAL
        partial_reasons.append("TEST_FILE_LIMIT_REACHED")
    test_candidates = all_test_candidates[:MAX_VERIFIER_TEST_FILES]

    for test_path in test_candidates:
        if total_bytes >= MAX_VERIFIER_TOTAL_BYTES:
            completeness = ContextCompleteness.PARTIAL
            partial_reasons.append("CONTEXT_BUDGET_REACHED")
            break
        if len(test_files) >= MAX_VERIFIER_TEST_FILES:
            break
        seen_paths.add(test_path)
        outcome, content, error_tag = await _fetch_single_file(
            client, owner, repo_name, test_path, ref=finding.commit_sha
        )
        if outcome == FetchOutcome.FOUND and content is not None:
            safe_c, _ = truncate_utf8_bytes(content, MAX_VERIFIER_FILE_BYTES)
            rc_f = RepoContextFile(path=test_path, content=safe_c, size_bytes=len(safe_c.encode("utf-8")))
            test_files.append(rc_f)
            total_bytes += rc_f.size_bytes
        elif outcome == FetchOutcome.INCOMPLETE:
            completeness = ContextCompleteness.PARTIAL
            partial_reasons.append(f"TRANSIENT_FAILURE:{test_path}:{error_tag}")
            omissions.append(f"Could not inspect test file {test_path} due to {error_tag}")

    # 6. README
    readme_str = ""
    outcome, raw_readme, error_tag = await _fetch_single_file(
        client, owner, repo_name, "README.md", ref=finding.commit_sha
    )
    if outcome == FetchOutcome.FOUND and raw_readme is not None:
        safe_readme, _ = truncate_utf8_bytes(raw_readme, MAX_VERIFIER_FILE_BYTES)
        readme_str = safe_readme
        total_bytes += len(safe_readme.encode("utf-8"))
    elif outcome == FetchOutcome.INCOMPLETE:
        completeness = ContextCompleteness.PARTIAL
        partial_reasons.append(f"TRANSIENT_FAILURE:README.md:{error_tag}")
        omissions.append(f"Could not inspect README.md due to {error_tag}")

    # 7. Existing Open Issues
    existing_issues: list[ExistingIssue] = []
    try:
        raw_issues = await client.get_issues(owner, repo_name, state="open", per_page=50)
        for issue in raw_issues:
            body_summary, _ = truncate_utf8_bytes(issue.get("body") or "", 200)
            existing_issues.append(
                ExistingIssue(
                    number=issue.get("number", 0),
                    title=issue.get("title", ""),
                    state=issue.get("state", "open"),
                    body_summary=body_summary,
                )
            )
    except Exception as exc:
        completeness = ContextCompleteness.PARTIAL
        partial_reasons.append(f"ISSUES_FETCH_FAILED:{type(exc).__name__}")
        omissions.append("Existing open issues could not be retrieved")

    return RepoContext(
        repository_id=finding.repository_id,
        installation_id=finding.installation_id,
        owner=owner,
        name=repo_name,
        commit_sha=finding.commit_sha,
        files=files,
        readme=readme_str,
        existing_issues=existing_issues,
        test_files=test_files,
        caller_files=caller_files,
        total_context_bytes=total_bytes,
        context_completeness=completeness,
        partial_reasons=list(set(partial_reasons)),
        omissions=list(set(omissions)),
    )
