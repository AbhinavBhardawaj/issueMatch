import ast
import re
from pathlib import Path
from typing import Set

from app.github.client import GitHubNotFoundError
from app.github.events import GitHubPushEvent
from app.github.scout_repository import ScoutGitHubReadClient
from app.scout.models import (
    ScoutContext,
    ScoutFile,
    ScoutExistingIssue,
    ChangedFile,
    ContextCompleteness,
)

# Explicit context bounds
MAX_CHANGED_FILES = 25
MAX_RELATED_FILES = 15
MAX_TEST_FILES = 4
MAX_FILE_BYTES = 20_000
MAX_PATCH_BYTES = 6_000
MAX_TOTAL_PATCH_BYTES = 20_000  # per-Scout-context / per-batch limit
MAX_README_BYTES = 8_000
MAX_ISSUES_TOTAL_BYTES = 8_000
MAX_TOTAL_CONTEXT_BYTES = 80_000
MAX_SCOUT_REQUEST_BYTES = 96_000  # IssueMatch conservative safety limit below provider 413
MAX_EXISTING_ISSUES = 50

SUPPORTED_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go"}
IGNORED_PATTERNS = {
    "node_modules/",
    "vendor/",
    ".git/",
    "dist/",
    "build/",
    "coverage/",
    "generated/",
    ".pytest_cache/",
}


def _is_supported_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    for pattern in IGNORED_PATTERNS:
        if pattern in normalized:
            return False
    suffix = Path(path).suffix.lower()
    return suffix in SUPPORTED_EXTENSIONS or Path(path).name.lower() == "readme.md"


def _is_test_file(path: str) -> bool:
    p = path.lower().replace("\\", "/")
    return (
        "test" in p
        or p.startswith("tests/")
        or "/tests/" in p
        or p.endswith("_test.py")
        or p.endswith(".test.js")
        or p.endswith(".spec.ts")
    )


def _get_test_stem(test_path: str) -> str:
    """Extract core module stem from test filename, e.g. test_agent.py -> agent."""
    name = Path(test_path).stem.lower()
    if name.startswith("test_"):
        return name[5:]
    if name.endswith("_test"):
        return name[:-5]
    return name


def _rank_relevant_test_files(
    candidate_paths: list[str],
    changed_paths: list[str],
    potential_imports: Set[str],
) -> list[str]:
    """
    Ranks candidate test paths by relevance to the changed files:
    Tier 1: Changed test itself
    Tier 2: Matching source/test stem (agent.py <-> test_agent.py)
    Tier 3: Direct import/reference relationship
    Tier 4: Package/module correspondence (shared package path)
    Tier 5: Same-directory fallback (only if capacity remains)
    Excludes test files with zero relationship to changed files.
    """
    changed_stems = {Path(p).stem.lower(): p for p in changed_paths}
    changed_dirs = {str(Path(p).parent).replace("\\", "/").lower() for p in changed_paths}
    scored: list[tuple[int, str]] = []

    for test_path in candidate_paths:
        norm_path = test_path.replace("\\", "/")
        norm_test = norm_path.lower()
        test_stem = _get_test_stem(norm_path)
        test_dir = str(Path(norm_path).parent).lower()

        # Tier 1: Changed test itself
        if test_path in changed_paths:
            scored.append((1, norm_path))
            continue

        # Tier 2: Matching stem
        if test_stem in changed_stems:
            scored.append((2, norm_path))
            continue

        # Tier 3: Direct import/reference
        if any(norm_path.endswith(rel) for rel in potential_imports):
            scored.append((3, norm_path))
            continue

        # Tier 4: Package/module correspondence
        matched_pkg = False
        for cdir in changed_dirs:
            # If changed file is app/scout/agent.py, tests in tests/scout/ match
            cdir_parts = [p for p in cdir.split("/") if p not in ("", "app", "src")]
            if cdir_parts and any(part in test_dir for part in cdir_parts):
                scored.append((4, norm_path))
                matched_pkg = True
                break
        if matched_pkg:
            continue

        # Tier 5: Same-directory fallback
        if test_dir in changed_dirs:
            scored.append((5, norm_path))
            continue

        # Otherwise: test is unrelated, discard!

    # Deterministic sort by (tier, path)
    scored.sort(key=lambda item: (item[0], item[1]))
    return [path for _, path in scored]


def _extract_imports(file_path: str, content: str) -> Set[str]:
    """Static AST discovery for Python and regex for JS/TS without executing repo code."""
    related: Set[str] = set()
    suffix = Path(file_path).suffix.lower()

    if suffix == ".py":
        try:
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        parts = alias.name.split(".")
                        related.add(parts[-1] + ".py")
                        related.add("/".join(parts) + ".py")
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        parts = node.module.split(".")
                        related.add(parts[-1] + ".py")
                        related.add("/".join(parts) + ".py")
                        for alias in node.names:
                            related.add(alias.name + ".py")
                            related.add("/".join(parts + [alias.name]) + ".py")
        except Exception:
            pass
    elif suffix in {".js", ".jsx", ".ts", ".tsx"}:
        # Look for import x from './path' or require('./path')
        matches = re.findall(r'(?:import|from|require)\s*\(?[\'"](\.[^\'"]+)[\'"]', content)
        for m in matches:
            cleaned = m.lstrip("./")
            for ext in [".js", ".ts", ".jsx", ".tsx"]:
                related.add(cleaned + ext)

    return related


def truncate_utf8_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    """
    Safely truncate a string to at most max_bytes when encoded in UTF-8.
    Never produces invalid UTF-8 and never splits multi-byte code points.
    Returns (truncated_text, was_truncated).
    """
    raw_bytes = text.encode("utf-8")
    if len(raw_bytes) <= max_bytes:
        return text, False
    truncated_bytes = raw_bytes[:max_bytes]
    truncated_text = truncated_bytes.decode("utf-8", errors="ignore")
    return truncated_text, True


class ScoutContextBuilder:
    def __init__(self, client: ScoutGitHubReadClient) -> None:
        self.client = client

    async def build_context(
        self,
        event: GitHubPushEvent,
        changed_files: list[ChangedFile],
        is_initial_push: bool = False,
        is_forced_push: bool = False,
        tree_truncated: bool = False,
    ) -> ScoutContext:
        partial_reasons: list[str] = []
        completeness = ContextCompleteness.COMPLETE

        if tree_truncated:
            completeness = ContextCompleteness.PARTIAL
            partial_reasons.append("TREE_TRUNCATED")

        if is_forced_push:
            completeness = ContextCompleteness.PARTIAL
            partial_reasons.append("FORCED_PUSH_FALLBACK")

        if len(changed_files) > MAX_CHANGED_FILES:
            completeness = ContextCompleteness.PARTIAL
            partial_reasons.append("CHANGED_FILE_LIMIT_REACHED")

        total_bytes = 0
        total_patch_bytes = 0
        collected_files: list[ScoutFile] = []
        collected_test_files: list[ScoutFile] = []
        seen_paths: Set[str] = set()
        changed_paths = [cf.path for cf in changed_files]

        # 1. Collect Changed Source Files
        for cf in changed_files[:MAX_CHANGED_FILES]:
            if not _is_supported_path(cf.path):
                continue
            if cf.path.lower() == "readme.md":
                continue

            is_test = _is_test_file(cf.path)
            if is_test and len(collected_test_files) >= MAX_TEST_FILES:
                completeness = ContextCompleteness.PARTIAL
                partial_reasons.append("TEST_FILE_LIMIT_REACHED")
                continue

            try:
                raw_content = await self.client.get_file_content(
                    event.repository_owner,
                    event.repository_name,
                    cf.path,
                    ref=event.after_sha,
                )
                safe_content, truncated = truncate_utf8_bytes(raw_content, MAX_FILE_BYTES)
                if truncated:
                    completeness = ContextCompleteness.PARTIAL
                    partial_reasons.append("FILE_TRUNCATED")

                content_bytes = len(safe_content.encode("utf-8"))
                if total_bytes + content_bytes > MAX_TOTAL_CONTEXT_BYTES:
                    completeness = ContextCompleteness.PARTIAL
                    partial_reasons.append("CONTEXT_BUDGET_REACHED")
                    break

                # Deterministic Per-Patch Bounding (Layer 1)
                safe_patch = None
                patch_bytes = 0
                if cf.patch:
                    safe_patch, patch_truncated = truncate_utf8_bytes(cf.patch, MAX_PATCH_BYTES)
                    remaining_patch_budget = max(0, MAX_TOTAL_PATCH_BYTES - total_patch_bytes)
                    safe_patch, over_patch_budget = truncate_utf8_bytes(safe_patch, remaining_patch_budget)
                    if patch_truncated or over_patch_budget:
                        completeness = ContextCompleteness.PARTIAL
                        partial_reasons.append("PATCH_TRUNCATED")
                    patch_bytes = len(safe_patch.encode("utf-8")) if safe_patch else 0
                    total_patch_bytes += patch_bytes

                scout_file = ScoutFile(
                    path=cf.path,
                    content=safe_content,
                    changed=True,
                    patch=safe_patch,
                    truncated=truncated,
                    size_bytes=content_bytes,
                )
                total_bytes += (content_bytes + patch_bytes)
                seen_paths.add(cf.path)

                if is_test:
                    collected_test_files.append(scout_file)
                else:
                    collected_files.append(scout_file)
            except Exception:
                completeness = ContextCompleteness.PARTIAL
                partial_reasons.append("FILE_FETCH_FAILED")

        # 2. Collect Related Files & Tests via static import discovery and relevance ranking
        potential_related: Set[str] = set()
        for f in collected_files:
            potential_related.update(_extract_imports(f.path, f.content))

        try:
            tree_items, truncated_flag = await self.client.get_repository_tree(
                event.repository_owner, event.repository_name, event.after_sha
            )
            if truncated_flag:
                completeness = ContextCompleteness.PARTIAL
                partial_reasons.append("TREE_TRUNCATED")

            tree_paths = [item["path"] for item in tree_items if "path" in item]
        except Exception:
            completeness = ContextCompleteness.PARTIAL
            partial_reasons.append("TREE_LOOKUP_FAILED")
            tree_paths = []

        # Separate candidates into related source candidates and test candidates
        related_candidates = [
            p for p in tree_paths
            if p not in seen_paths
            and _is_supported_path(p)
            and not _is_test_file(p)
            and any(p.endswith(rel) for rel in potential_related)
        ]

        test_candidates = [
            p for p in tree_paths
            if p not in seen_paths
            and _is_supported_path(p)
            and _is_test_file(p)
        ]

        # Rank tests deterministically by relevance to changed files
        ranked_test_paths = _rank_relevant_test_files(
            candidate_paths=test_candidates,
            changed_paths=changed_paths,
            potential_imports=potential_related,
        )

        # Fetch related source files
        related_count = 0
        for path in related_candidates[:MAX_RELATED_FILES]:
            if related_count >= MAX_RELATED_FILES:
                completeness = ContextCompleteness.PARTIAL
                partial_reasons.append("RELATED_FILE_LIMIT_REACHED")
                break

            try:
                raw_content = await self.client.get_file_content(
                    event.repository_owner,
                    event.repository_name,
                    path,
                    ref=event.after_sha,
                )
                safe_content, truncated = truncate_utf8_bytes(raw_content, MAX_FILE_BYTES)
                if truncated:
                    completeness = ContextCompleteness.PARTIAL
                    partial_reasons.append("FILE_TRUNCATED")

                content_bytes = len(safe_content.encode("utf-8"))
                if total_bytes + content_bytes > MAX_TOTAL_CONTEXT_BYTES:
                    completeness = ContextCompleteness.PARTIAL
                    partial_reasons.append("CONTEXT_BUDGET_REACHED")
                    break

                scout_file = ScoutFile(
                    path=path,
                    content=safe_content,
                    changed=False,
                    patch=None,
                    truncated=truncated,
                    size_bytes=content_bytes,
                )
                total_bytes += scout_file.size_bytes
                seen_paths.add(path)
                collected_files.append(scout_file)
                related_count += 1
            except Exception:
                completeness = ContextCompleteness.PARTIAL
                partial_reasons.append("FILE_FETCH_FAILED")

        # Fetch relevant tests (strictly bounded by MAX_TEST_FILES = 4)
        for path in ranked_test_paths:
            if len(collected_test_files) >= MAX_TEST_FILES:
                completeness = ContextCompleteness.PARTIAL
                partial_reasons.append("TEST_FILE_LIMIT_REACHED")
                break

            try:
                raw_content = await self.client.get_file_content(
                    event.repository_owner,
                    event.repository_name,
                    path,
                    ref=event.after_sha,
                )
                safe_content, truncated = truncate_utf8_bytes(raw_content, MAX_FILE_BYTES)
                if truncated:
                    completeness = ContextCompleteness.PARTIAL
                    partial_reasons.append("FILE_TRUNCATED")

                content_bytes = len(safe_content.encode("utf-8"))
                if total_bytes + content_bytes > MAX_TOTAL_CONTEXT_BYTES:
                    completeness = ContextCompleteness.PARTIAL
                    partial_reasons.append("CONTEXT_BUDGET_REACHED")
                    break

                scout_file = ScoutFile(
                    path=path,
                    content=safe_content,
                    changed=False,
                    patch=None,
                    truncated=truncated,
                    size_bytes=content_bytes,
                )
                total_bytes += scout_file.size_bytes
                seen_paths.add(path)
                collected_test_files.append(scout_file)
            except Exception:
                completeness = ContextCompleteness.PARTIAL
                partial_reasons.append("FILE_FETCH_FAILED")

        # 3. Collect README obeying remaining budget
        readme_content = ""
        remaining_for_readme = min(MAX_README_BYTES, max(0, MAX_TOTAL_CONTEXT_BYTES - total_bytes))
        if remaining_for_readme > 0:
            try:
                raw_readme = await self.client.get_file_content(
                    event.repository_owner,
                    event.repository_name,
                    "README.md",
                    ref=event.after_sha,
                )
                readme_content, readme_truncated = truncate_utf8_bytes(raw_readme, remaining_for_readme)
                if readme_truncated:
                    completeness = ContextCompleteness.PARTIAL
                    partial_reasons.append("README_TRUNCATED")
                total_bytes += len(readme_content.encode("utf-8"))
            except GitHubNotFoundError:
                readme_content = ""
            except Exception:
                completeness = ContextCompleteness.PARTIAL
                partial_reasons.append("README_FETCH_FAILED")
                readme_content = ""
        else:
            completeness = ContextCompleteness.PARTIAL
            partial_reasons.append("README_TRUNCATED")

        # 4. Collect Existing Open Issues obeying issues budget
        existing_issues: list[ScoutExistingIssue] = []
        issues_bytes_used = 0
        try:
            raw_issues = await self.client.get_issues(
                event.repository_owner,
                event.repository_name,
                state="open",
                per_page=MAX_EXISTING_ISSUES,
            )
            for issue in raw_issues:
                body_summary, _ = truncate_utf8_bytes(issue.get("body") or "", 200)
                item_est = len(body_summary.encode("utf-8")) + len(str(issue.get("title", "")).encode("utf-8")) + 64
                if issues_bytes_used + item_est > MAX_ISSUES_TOTAL_BYTES:
                    completeness = ContextCompleteness.PARTIAL
                    partial_reasons.append("ISSUES_TRUNCATED")
                    break
                existing_issues.append(
                    ScoutExistingIssue(
                        number=issue.get("number", 0),
                        title=issue.get("title", ""),
                        state=issue.get("state", "open"),
                        body_summary=body_summary,
                    )
                )
                issues_bytes_used += item_est
        except Exception:
            completeness = ContextCompleteness.PARTIAL
            partial_reasons.append("ISSUES_FETCH_FAILED")
            existing_issues = []

        return ScoutContext(
            installation_id=event.installation_id,
            repository_id=event.repository_id,
            owner=event.repository_owner,
            name=event.repository_name,
            before_sha=event.before_sha,
            commit_sha=event.after_sha,
            default_branch=event.default_branch,
            files=collected_files,
            test_files=collected_test_files,
            readme=readme_content,
            existing_issues=existing_issues,
            changed_paths=changed_paths,
            total_context_bytes=total_bytes,
            context_completeness=completeness,
            partial_reasons=list(set(partial_reasons)),
        )
