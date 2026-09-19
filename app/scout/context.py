import ast
import re
from pathlib import Path
from typing import Set

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
MAX_TEST_FILES = 10
MAX_FILE_BYTES = 20_000
MAX_TOTAL_CONTEXT_BYTES = 160_000
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
    p = path.lower()
    return (
        "test" in p
        or p.startswith("tests/")
        or p.endswith("_test.py")
        or p.endswith(".test.js")
        or p.endswith(".spec.ts")
    )


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

                scout_file = ScoutFile(
                    path=cf.path,
                    content=safe_content,
                    changed=True,
                    patch=cf.patch,
                    truncated=truncated,
                    size_bytes=content_bytes,
                )
                total_bytes += scout_file.size_bytes
                seen_paths.add(cf.path)

                if is_test:
                    collected_test_files.append(scout_file)
                else:
                    collected_files.append(scout_file)
            except Exception:
                completeness = ContextCompleteness.PARTIAL
                partial_reasons.append("FILE_FETCH_FAILED")

        # 2. Collect Related Files & Tests via static import discovery
        potential_related: Set[str] = set()
        for f in collected_files:
            potential_related.update(_extract_imports(f.path, f.content))

        # Check repository tree for matches to potential related imports
        try:
            tree_items, truncated_flag = await self.client.get_repository_tree(
                event.repository_owner, event.repository_name, event.after_sha
            )
            if truncated_flag:
                completeness = ContextCompleteness.PARTIAL
                partial_reasons.append("TREE_TRUNCATED")

            tree_paths = [item["path"] for item in tree_items if "path" in item]
        except Exception:
            tree_paths = []

        related_count = 0
        for path in tree_paths:
            if path in seen_paths or not _is_supported_path(path):
                continue

            matches_import = any(path.endswith(rel) for rel in potential_related)
            is_test = _is_test_file(path)

            if matches_import or is_test:
                if is_test:
                    if len(collected_test_files) >= MAX_TEST_FILES:
                        completeness = ContextCompleteness.PARTIAL
                        partial_reasons.append("TEST_FILE_LIMIT_REACHED")
                        continue
                else:
                    if related_count >= MAX_RELATED_FILES:
                        completeness = ContextCompleteness.PARTIAL
                        partial_reasons.append("RELATED_FILE_LIMIT_REACHED")
                        continue

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

                    if is_test:
                        collected_test_files.append(scout_file)
                    else:
                        collected_files.append(scout_file)
                        related_count += 1
                except Exception:
                    completeness = ContextCompleteness.PARTIAL
                    partial_reasons.append("FILE_FETCH_FAILED")

        # 3. Collect README
        readme_content = ""
        try:
            raw_readme = await self.client.get_file_content(
                event.repository_owner,
                event.repository_name,
                "README.md",
                ref=event.after_sha,
            )
            readme_content, readme_truncated = truncate_utf8_bytes(raw_readme, MAX_FILE_BYTES)
            if readme_truncated:
                completeness = ContextCompleteness.PARTIAL
                partial_reasons.append("README_TRUNCATED")
            total_bytes += len(readme_content.encode("utf-8"))
        except Exception:
            readme_content = ""

        # 4. Collect Existing Open Issues
        existing_issues: list[ScoutExistingIssue] = []
        try:
            raw_issues = await self.client.get_issues(
                event.repository_owner,
                event.repository_name,
                state="open",
                per_page=MAX_EXISTING_ISSUES,
            )
            for issue in raw_issues:
                body_summary, _ = truncate_utf8_bytes(issue.get("body") or "", 200)
                existing_issues.append(
                    ScoutExistingIssue(
                        number=issue.get("number", 0),
                        title=issue.get("title", ""),
                        state=issue.get("state", "open"),
                        body_summary=body_summary,
                    )
                )
        except Exception:
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
