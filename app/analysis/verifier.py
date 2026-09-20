"""Conservative citation checks against code actually supplied to the model."""
import re

from app.models.candidate import CandidateSubmission
from app.models.context import RepositoryAnalysisContext

from .provider import EvidenceCitation


def normalize_evidence_path(path: str, context: RepositoryAnalysisContext) -> str:
    """Normalize only explicit repository prefixes, never guess a file by excerpt."""
    value = path.strip().lstrip("/")
    repository = context.repository_context
    for prefix in (f"{repository.owner}/{repository.name}/", f"{repository.name}/"):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def resolve_evidence_path(item: EvidenceCitation, context: RepositoryAnalysisContext) -> str | None:
    """Resolve an exact path or a unique same-basename file with an exact excerpt."""
    raw_path = item.path.strip()
    if raw_path.startswith("/") or ".." in raw_path.split("/"):
        return None
    files = {file.path: file for file in (*context.code_context.file_contents,
                                         *context.code_context.test_files)}
    path = normalize_evidence_path(raw_path, context)
    if path in files:
        return path
    basename = path.rsplit("/", 1)[-1]
    excerpt = item.excerpt.strip()
    if not excerpt:
        return None
    matches = [file.path for file in files.values()
               if file.path.rsplit("/", 1)[-1] == basename and excerpt in file.content]
    return matches[0] if len(matches) == 1 else None


def verify_evidence(evidence: list[EvidenceCitation], context: RepositoryAnalysisContext) -> list[str]:
    """Verify exact excerpts, not the semantic truth of model claims.

    Truncated files are checked only against the supplied prefix; unseen content
    cannot be cited. A verified excerpt is not proof of its interpretation.
    """
    files = {file.path: file for file in (*context.code_context.file_contents, *context.code_context.test_files)}
    verified: list[str] = []
    for item in evidence:
        resolved_path = resolve_evidence_path(item, context)
        file = files.get(resolved_path) if resolved_path else None
        excerpt = item.excerpt.strip()
        claim = item.claim.strip()
        if file is None:
            continue
        if not excerpt or not claim or excerpt not in file.content:
            continue
        line_number = file.content[:file.content.find(excerpt)].count("\n") + 1
        # Do not echo repository source: a cited line may contain a credential.
        verified.append(f"{file.path}:L{line_number} (source excerpt verified)")
    return verified


_GENERIC_WORDS = {
    "add", "and", "bug", "change", "code", "fix", "for", "from", "issue",
    "modify", "not", "return", "should", "test", "the", "this", "update",
    "use", "using", "when", "with", "work", "would",
}


def derive_issue_grounded_evidence(
    candidate: CandidateSubmission, context: RepositoryAnalysisContext, limit: int = 4,
) -> list[str]:
    """Find source locations mentioned by both issue and approach.

    This is a grounding fallback for a model PASS with *no* citations, not a
    semantic proof or a way to rehabilitate invented citations. It never
    copies source contents (including possible secrets) to a GitHub reply.
    """
    def terms(value: str) -> set[str]:
        return {word for word in re.findall(r"[a-z_][a-z_0-9]*|\b\d{3}\b", value.lower())
                if len(word) >= 3 and word not in _GENERIC_WORDS}

    issue = context.issue_context
    shared = terms(candidate.approach) & terms(f"{issue.title} {issue.body}")
    if not shared:
        return []

    matches: list[tuple[int, str, int, set[str]]] = []
    for file in context.code_context.file_contents:
        for line_number, line in enumerate(file.content.splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            overlap = shared & terms(line)
            if overlap:
                matches.append((len(overlap), file.path, line_number, overlap))

    matches.sort(key=lambda item: (-item[0], item[1], item[2]))
    selected: list[str] = []
    covered: set[str] = set()
    for _, path, line_number, overlap in matches:
        new = overlap - covered
        if not new:
            continue
        selected.append(f"{path}:L{line_number} (issue/approach terms: {', '.join(sorted(new)[:3])})")
        covered.update(new)
        if len(selected) >= limit:
            break
    return selected
