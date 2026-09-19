import asyncio
from app.domain.models import Finding, RepoContext, RepoContextFile, ExistingIssue
from app.github.client import GitHubClient, GitHubClientError

async def fetch_repo_context(
    finding: Finding,
    client: GitHubClient,
    owner: str,
    repo_name: str,
) -> RepoContext:
    """Fetch files and existing issues from GitHub to build a RepoContext."""
    
    unique_paths = {finding.file}
    for item in finding.evidence:
        unique_paths.add(item.file)
    
    # Always try to fetch README.md
    unique_paths.add("README.md")
    
    async def fetch_file(path: str) -> RepoContextFile | None:
        try:
            content = await client.get_file_content(owner, repo_name, path, finding.commit_sha)
            return RepoContextFile(path=path, content=content, size_bytes=len(content.encode('utf-8')))
        except (GitHubClientError, Exception):
            # Missing or inaccessible files are omitted from files list.
            # Downstream validate_evidence will mark evidence CONTRADICTED if evidence file is missing.
            return None
            
    file_tasks = [fetch_file(p) for p in unique_paths]
    fetched_files = await asyncio.gather(*file_tasks)
    
    readme = None
    files = []
    for f in fetched_files:
        if f is None:
            continue
        if f.path.lower() == "readme.md":
            readme = f
        else:
            files.append(f)
            
    # If no README was found (size 0), we set it to empty string
    readme_str = ""
    if readme and readme.size_bytes > 0:
        readme_str = readme.content
        
    try:
        raw_issues = await client.get_issues(owner, repo_name, state="open", per_page=50)
        existing_issues = [
            ExistingIssue(
                number=issue.get("number", 0),
                title=issue.get("title", ""),
                state=issue.get("state", "open"),
                body_summary=(issue.get("body") or "")[:200]
            )
            for issue in raw_issues
        ]
    except GitHubClientError:
        existing_issues = []
        
    return RepoContext(
        repository_id=finding.repository_id,
        installation_id=finding.installation_id,
        owner=owner,
        name=repo_name,
        commit_sha=finding.commit_sha,
        files=files,
        readme=readme_str,
        existing_issues=existing_issues
    )
