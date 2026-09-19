"""Safe rendering and posting of structured analysis recommendations."""
import re
from app.github.client import GitHubClient
from app.models.analysis import AnalysisDecision, ApproachAnalysis
from app.models.candidate import CandidateSubmission

_MD = re.compile(r"([\\`*_{}\[\]()<>#])")

def _escape(value: str) -> str: return _MD.sub(r"\\\1", value or "")

def render_candidate_analysis(candidate: CandidateSubmission, analysis: ApproachAnalysis) -> str:
    """Render data as Markdown, never as executable instructions or assignment commands."""
    username = re.sub(r"[^A-Za-z0-9-]", "", candidate.contributor_username) or "contributor"
    decision_label = {
        AnalysisDecision.PASS: "ACCEPTED",
        AnalysisDecision.REVISION_REQUIRED: "REVISION_REQUIRED",
        AnalysisDecision.REJECT: "DECLINED",
    }[analysis.decision]
    lines = [f"Candidate: @{username}", "", f"Status - {decision_label}", ""]
    if analysis.decision is AnalysisDecision.PASS:
        lines.extend(["Evidence:", *[f"- {_escape(item)}" for item in analysis.evidence], "", f"Recommendation: {_escape(analysis.recommendation) or 'Maintainer may assign this issue to the contributor.'}", "", "The maintainer decides whether to assign the issue."])
    elif analysis.decision is AnalysisDecision.REVISION_REQUIRED:
        lines.extend(["Feedback:", _escape(analysis.revision_feedback), "", "Please revise your approach and submit it again."])
    else:
        lines.extend(["Review notes:", *[f"- {_escape(item)}" for item in analysis.issues], "", _escape(analysis.recommendation or "The proposed approach does not match the issue requirements.")])
    return "\n".join(lines)

async def post_candidate_analysis(client: GitHubClient, candidate: CandidateSubmission, analysis: ApproachAnalysis) -> dict:
    return await client.create_issue_comment(candidate.repository_owner, candidate.repository_name, candidate.issue_number, render_candidate_analysis(candidate, analysis))


async def post_analysis_unavailable(client: GitHubClient, candidate: CandidateSubmission) -> dict:
    """Acknowledge an approach without inventing an analysis decision."""
    username = re.sub(r"[^A-Za-z0-9-]", "", candidate.contributor_username) or "contributor"
    body = (
        f"Candidate: @{username}\n\n"
        "Analysis temporarily unavailable. Your approach was received, but the bot could not "
        "complete a reliable evaluation. No ACCEPTED, REVISION_REQUIRED, or DECLINED decision "
        "has been made. Please do not revise your proposal based only on this notice.\n\n"
        "Maintainer: once the analysis service is available, ask for a new approach comment "
        "to trigger evaluation."
    )
    return await client.create_issue_comment(candidate.repository_owner, candidate.repository_name,
                                             candidate.issue_number, body)
