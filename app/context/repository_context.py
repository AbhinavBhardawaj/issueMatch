"""Repository metadata and bounded tree collection."""
from app.github.client import GitHubClient
from app.models.context import RepositoryContext

MAX_TREE_PATHS = 500

async def collect_repository_context(client: GitHubClient, owner: str, repository: str, default_branch: str | None = None) -> RepositoryContext:
    metadata = await client.get_repository(owner, repository)
    branch = default_branch or metadata.get("default_branch") or "main"
    languages = await client.get_languages(owner, repository)
    tree, upstream_truncated = await client.get_repository_tree(owner, repository, branch)
    paths = [item["path"] for item in tree if isinstance(item, dict) and item.get("type") == "blob" and isinstance(item.get("path"), str)]
    bounded_paths = paths[:MAX_TREE_PATHS]
    top_level = sorted({path.split("/", 1)[0] for path in bounded_paths})
    return RepositoryContext(owner=owner, name=repository, default_branch=branch,
        description=metadata.get("description") or "", languages={str(k): int(v) for k, v in languages.items() if isinstance(v, int)},
        tree_paths=bounded_paths, top_level_entries=top_level, tree_truncated=upstream_truncated or len(paths) > MAX_TREE_PATHS)
