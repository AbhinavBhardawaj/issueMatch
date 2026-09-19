# NOTE: Unrelated to app.analysis.verifier

import ast
import pathlib
from pydantic import BaseModel, ConfigDict
from app.domain.states import EvidenceStatus
from app.domain.models import Finding, RepoContext, RepoContextFile

class EvidenceItemResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    
    file: str
    line: int
    snippet: str
    file_exists: bool
    line_exists: bool
    snippet_found: bool
    function_exists: bool | None = None
    status: EvidenceStatus

class EvidenceValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    
    finding_id: str
    commit_sha: str
    results: list[EvidenceItemResult]
    overall: EvidenceStatus

ANALYSABLE_EXTENSIONS: set[str] = {".py"}

def _can_analyse_functions(file_path: str) -> bool:
    return pathlib.Path(file_path).suffix in ANALYSABLE_EXTENSIONS

def _check_file_exists(file_path: str, files: list[RepoContextFile]) -> tuple[bool, str | None]:
    for f in files:
        if f.path == file_path:
            return True, f.content
    return False, None

def _check_line_exists(content: str, line: int) -> bool:
    lines = content.split('\n')
    return 1 <= line <= len(lines)

def _check_snippet_found(content: str, line: int, snippet: str) -> bool:
    lines = content.split('\n')
    # 1-indexed line to 0-indexed index
    line_idx = line - 1
    start_idx = max(0, line_idx - 5)
    end_idx = min(len(lines), line_idx + 6)
    
    snippet_stripped = snippet.strip()
    
    for i in range(start_idx, end_idx):
        if snippet_stripped in lines[i]:
            return True
    return False

def _check_function_exists(content: str, function_name: str, target_line: int | None = None) -> bool | None:
    try:
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == function_name:
                    if target_line is not None:
                        end_lineno = getattr(node, "end_lineno", None)
                        if end_lineno is not None:
                            if node.lineno <= target_line <= end_lineno:
                                return True
                            else:
                                continue
                        else:
                            return True
                    return True
        return False
    except SyntaxError:
        return None
    except Exception:
        return None

def validate_evidence(finding: Finding, repo_context: RepoContext) -> EvidenceValidationResult:
    results = []
    
    for evidence_item in finding.evidence:
        file_exists, content = _check_file_exists(evidence_item.file, repo_context.files)
        
        if not file_exists:
            results.append(EvidenceItemResult(
                file=evidence_item.file,
                line=evidence_item.line,
                snippet=evidence_item.snippet,
                file_exists=False,
                line_exists=False,
                snippet_found=False,
                function_exists=None,
                status=EvidenceStatus.CONTRADICTED
            ))
            continue
            
        line_exists = _check_line_exists(content, evidence_item.line)
        if not line_exists:
            results.append(EvidenceItemResult(
                file=evidence_item.file,
                line=evidence_item.line,
                snippet=evidence_item.snippet,
                file_exists=True,
                line_exists=False,
                snippet_found=False,
                function_exists=None,
                status=EvidenceStatus.CONTRADICTED
            ))
            continue
            
        snippet_found = _check_snippet_found(content, evidence_item.line, evidence_item.snippet)
        if not snippet_found:
            results.append(EvidenceItemResult(
                file=evidence_item.file,
                line=evidence_item.line,
                snippet=evidence_item.snippet,
                file_exists=True,
                line_exists=True,
                snippet_found=False,
                function_exists=None,
                status=EvidenceStatus.CONTRADICTED
            ))
            continue
            
        function_exists = None
        if finding.function and _can_analyse_functions(evidence_item.file):
            function_exists = _check_function_exists(content, finding.function, target_line=evidence_item.line)
            if function_exists is False:
                results.append(EvidenceItemResult(
                    file=evidence_item.file,
                    line=evidence_item.line,
                    snippet=evidence_item.snippet,
                    file_exists=True,
                    line_exists=True,
                    snippet_found=True,
                    function_exists=False,
                    status=EvidenceStatus.CONTRADICTED
                ))
                continue
                
        # If we got here, it's either SUPPORTED or UNKNOWN
        if function_exists is None and finding.function and _can_analyse_functions(evidence_item.file):
            status = EvidenceStatus.UNKNOWN
        else:
            status = EvidenceStatus.SUPPORTED
            
        results.append(EvidenceItemResult(
            file=evidence_item.file,
            line=evidence_item.line,
            snippet=evidence_item.snippet,
            file_exists=True,
            line_exists=True,
            snippet_found=True,
            function_exists=function_exists,
            status=status
        ))
        
    overall = EvidenceStatus.SUPPORTED
    if any(r.status == EvidenceStatus.CONTRADICTED for r in results):
        overall = EvidenceStatus.CONTRADICTED
    elif any(r.status == EvidenceStatus.UNKNOWN for r in results):
        overall = EvidenceStatus.UNKNOWN
        
    return EvidenceValidationResult(
        finding_id=finding.finding_id,
        commit_sha=finding.commit_sha,
        results=results,
        overall=overall
    )
