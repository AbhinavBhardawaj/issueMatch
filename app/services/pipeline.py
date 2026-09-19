import logging
from app.domain.models import Finding, RepoContext
from app.domain.transitions import transition_finding
from app.domain.states import FindingStatus, VerificationStatus, EvidenceStatus
from app.verifier.evidence import validate_evidence
from app.verifier.agent import run_verifier, LLMProvider
from app.verifier.schemas import LLMInvocationError, MalformedVerifierResponse
from app.verifier.deterministic import post_verifier_recheck, RecheckStatus
from app.services.issue_gate import should_create_issue, GateDecision, InMemoryDedupStore
from app.services.issue_creator import create_issue_if_authorized, IssueWriteClient

logger = logging.getLogger(__name__)

class VerifierPipeline:
    def __init__(
        self,
        llm_provider: LLMProvider,
        dedup_store: InMemoryDedupStore | None = None,
    ):
        self.llm_provider = llm_provider
        self.dedup_store = dedup_store or InMemoryDedupStore()

    async def run(
        self,
        finding: Finding,
        repo_context: RepoContext,
        owner: str,
        repo_name: str,
        *,
        github_write_client: IssueWriteClient,
    ):

        # 0. Initial transition: DISCOVERED -> VERIFYING
        if finding.status == FindingStatus.DISCOVERED:
            finding = transition_finding(finding, FindingStatus.VERIFYING)

        # 1. Deterministic evidence validation
        evidence_result = validate_evidence(finding, repo_context)
        if evidence_result.overall == EvidenceStatus.CONTRADICTED:
            finding = transition_finding(finding, FindingStatus.REJECTED)
            logger.info(f"Finding {finding.finding_id} rejected during evidence validation")
            return None

        # 2. Adversarial LLM Verification
        try:
            verification = await run_verifier(finding, repo_context, self.llm_provider)
        except (LLMInvocationError, MalformedVerifierResponse) as e:
            logger.warning(f"Finding {finding.finding_id} verification failed: {str(e)}")
            transition_finding(finding, FindingStatus.VERIFICATION_FAILED)
            return None

        if verification.status != VerificationStatus.VERIFIED:
            finding = transition_finding(finding, FindingStatus.REJECTED)
            logger.info(f"Finding {finding.finding_id} rejected by LLM: {verification.reason}")
            return None

        # 3. Post-verifier recheck (performed while Finding is still in VERIFYING state)
        recheck = post_verifier_recheck(finding, verification, evidence_result, repo_context)
        if recheck.status == RecheckStatus.FAIL:
            finding = transition_finding(finding, FindingStatus.REJECTED)
            logger.info(f"Finding {finding.finding_id} rejected during post-verifier recheck: {recheck.reason}")
            return None

        # Only after successful recheck: transition to VERIFIED
        finding = transition_finding(finding, FindingStatus.VERIFIED)

        # 4. Dedup check
        dedup_result = self.dedup_store.check_and_reserve(finding)

        # 5. Gate decision
        gate_result = should_create_issue(finding, verification, evidence_result, repo_context, dedup_result)
        if gate_result.decision == GateDecision.DENY:
            logger.info(f"Finding {finding.finding_id} denied by gate: {gate_result.reason}")
            return None

        # 6. Issue Creation
        finding = transition_finding(finding, FindingStatus.ISSUE_CREATING)
        result = await create_issue_if_authorized(
            gate_result,
            dedup_result,
            finding,
            verification,
            github_write_client,
            owner,
            repo_name,
            evidence_result=evidence_result,
            repo_context=repo_context,
            dedup_store=self.dedup_store,
        )

        finding = transition_finding(finding, FindingStatus.ISSUE_CREATED)
        logger.info(f"Finding {finding.finding_id} successfully created issue: {result.issue_number}")

        return result
