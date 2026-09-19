"""Targeted, size-bounded source retrieval from approach and issue context."""
import re
from app.github.client import GitHubClient, GitHubNotFoundError
from app.models.context import CodeContext, CodeFile, IssueContext, RepositoryContext

MAX_FILES = 8
MAX_FILE_CHARS = 12_000
MAX_TOTAL_CHARS = 40_000
_PATH = re.compile(
    r"(?<![\w./-])((?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*"
    r"\.(?:py|js|jsx|ts|tsx|go|rs|java|rb|php|cs|cpp|c|h|md|yml|yaml|json)"
    r"|(?:[A-Za-z0-9_-]+/)*Dockerfile)(?![\w./-])"
)


def extract_mentioned_paths(approach: str) -> list[str]:
    """Extract plausible relative file paths, rejecting traversal and duplicates."""
    result: list[str] = []
    for match in _PATH.finditer(approach or ""):
        path = match.group(1)
        if path not in result:
            result.append(path)
    return result


async def collect_code_context(client: GitHubClient, owner: str, repository: str, approach: str,
                               repo_context: RepositoryContext, issue_context: IssueContext | None = None) -> CodeContext:
    """Prioritize approach paths, issue paths, then narrow tree matches, within strict caps."""
    available = set(repo_context.tree_paths)
    issue_text = f"{issue_context.title} {issue_context.body}" if issue_context else ""
    mentioned = list(dict.fromkeys(extract_mentioned_paths(approach) + extract_mentioned_paths(issue_text)))
    if not any(path in available for path in mentioned) and issue_text:
        # A narrow fallback for issues naming a component without its extension.
        words = set(re.findall(r"[a-z0-9_]+", issue_text.lower()))
        for path in repo_context.tree_paths:
            stem = path.rsplit("/", 1)[-1].split(".", 1)[0].lower()
            if stem in words and stem not in {"test", "tests", "readme", "index"}:
                mentioned.append(path)
                if len(mentioned) >= MAX_FILES:
                    break
    existing = [path for path in mentioned if path in available][:MAX_FILES]
    missing = [path for path in mentioned if path not in available]
    # A matching test path is useful context, but never fetch more than the global cap.
    candidates = list(existing)
    for path in existing:
        stem = path.rsplit(".", 1)[0].split("/")[-1]
        test_matches = [p for p in repo_context.tree_paths if ("test" in p.lower() or "spec" in p.lower()) and stem in p]
        for test_path in test_matches:
            if test_path not in candidates and len(candidates) < MAX_FILES:
                candidates.append(test_path)
    files: list[CodeFile] = []
    tests: list[CodeFile] = []
    total = 0
    for path in candidates:
        if total >= MAX_TOTAL_CHARS: break
        try:
            content = await client.get_file_content(owner, repository, path, repo_context.default_branch)
        except GitHubNotFoundError:
            if path not in missing: missing.append(path)
            continue
        remaining = min(MAX_FILE_CHARS, MAX_TOTAL_CHARS - total)
        truncated = len(content) > remaining
        file = CodeFile(path=path, content=content[:remaining], truncated=truncated)
        (tests if ("test" in path.lower() or "spec" in path.lower()) else files).append(file)
        total += len(file.content)
    return CodeContext(relevant_files=existing, file_contents=files, test_files=tests, missing_paths=missing,
        repository_tree_summary=repo_context.top_level_entries, total_characters=total)
