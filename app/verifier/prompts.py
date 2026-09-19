from app.domain.models import Finding, RepoContext

SYSTEM_PROMPT = """You are an adversarial code verification agent. Your objective is to actively attempt to DISPROVE the reported finding based on the provided repository context.
Investigate:
1. Is the code path actually reachable?
2. Is the behavior intentional or explicitly designed this way?
3. Is there a handler, middleware, or protection mechanism elsewhere that neutralizes the defect?
4. Does an existing test or specification establish the current behavior as expected?
5. Does existing documentation or type contracts contradict the finding?
6. Is the provided context insufficient to confirm the bug? (If so, use NEEDS_MORE_CONTEXT)
7. Does an existing open issue already track this exact defect?

You MUST output a JSON object exactly matching this schema:
{
  "status": "VERIFIED" | "REJECTED" | "NEEDS_MORE_CONTEXT",
  "reason": "Detailed string explaining why it is verified, rejected, or needs more context",
  "confidence": float between 0.0 and 1.0,
  "supporting_evidence": [{"file": "string", "line": integer, "snippet": "string"}],
  "counter_evidence": [{"file": "string", "line": integer, "snippet": "string"}],
  "duplicate_issue": boolean
}
Do not include any other text outside the JSON."""

def build_verifier_prompt(finding: Finding, repo_context: RepoContext) -> str:
    prompt = f"Finding:\nTitle: {finding.title}\nDescription: {finding.description}\nFile: {finding.file}\n"
    if finding.function:
        prompt += f"Function: {finding.function}\n"
    if finding.line:
        prompt += f"Line: {finding.line}\n"
        
    prompt += "\nEvidence:\n"
    for item in finding.evidence:
        prompt += f"- File: {item.file}, Line: {item.line}, Snippet: {item.snippet}\n"
        
    prompt += "\n<untrusted_repository_context>\n"
    for rc_file in repo_context.files:
        prompt += f"\n--- {rc_file.path} ---\n{rc_file.content}\n"
    prompt += "</untrusted_repository_context>\n"
        
    return prompt
