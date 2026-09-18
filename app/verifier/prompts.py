from app.domain.models import Finding, RepoContext

SYSTEM_PROMPT = "You are a code verification agent. Determine if the reported finding is valid based on the provided repository context."

def build_user_prompt(finding: Finding, repo_context: RepoContext) -> str:
    prompt = f"Finding:\nTitle: {finding.title}\nDescription: {finding.description}\nFile: {finding.file}\n"
    if finding.function:
        prompt += f"Function: {finding.function}\n"
    if finding.line:
        prompt += f"Line: {finding.line}\n"
        
    prompt += "\nEvidence:\n"
    for item in finding.evidence:
        prompt += f"- File: {item.file}, Line: {item.line}, Snippet: {item.snippet}\n"
        
    prompt += "\nRepository Context:\n"
    for rc_file in repo_context.files:
        prompt += f"\n--- {rc_file.path} ---\n{rc_file.content}\n"
        
    return prompt
