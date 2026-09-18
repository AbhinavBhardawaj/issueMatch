"""Bounded, GitHub-independent context supplied to analysis implementations."""
from pydantic import BaseModel, ConfigDict, Field


class IssueContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    repository_owner: str
    repository_name: str
    issue_number: int
    title: str = ""
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    state: str = "unknown"
    author: str | None = None
    author_id: int | None = None
    url: str | None = None


class RepositoryContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    owner: str
    name: str
    default_branch: str = "main"
    description: str = ""
    languages: dict[str, int] = Field(default_factory=dict)
    tree_paths: list[str] = Field(default_factory=list)
    top_level_entries: list[str] = Field(default_factory=list)
    tree_truncated: bool = False


class CodeFile(BaseModel):
    model_config = ConfigDict(frozen=True)
    path: str
    content: str
    truncated: bool = False


class CodeContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    relevant_files: list[str] = Field(default_factory=list)
    file_contents: list[CodeFile] = Field(default_factory=list)
    test_files: list[CodeFile] = Field(default_factory=list)
    missing_paths: list[str] = Field(default_factory=list)
    repository_tree_summary: list[str] = Field(default_factory=list)
    total_characters: int = 0


class RepositoryAnalysisContext(BaseModel):
    """Primary input contract for Developer B's analysis implementation."""

    model_config = ConfigDict(frozen=True)
    issue_context: IssueContext
    repository_context: RepositoryContext
    code_context: CodeContext
