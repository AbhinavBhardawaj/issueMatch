import logging
from typing import Callable, Any
from app.github.events import GitHubPushEvent
from app.github.scout_repository import ScoutGitHubReadClient, verify_repository_identity
from app.github.repo_fetcher import fetch_repo_context
from app.scout.models import ChangedFile, ScoutRunResult
from app.scout.context import ScoutContextBuilder
from app.scout.agent import ScoutAgent
from app.scout.worthiness import rank_and_filter_drafts, materialize_finding
from app.services.pipeline import VerifierPipeline

logger = logging.getLogger(__name__)

ALL_ZERO_SHA = "0" * 40


class ScoutService:
    """
    Local orchestration layer for Issue Scout.
    Connects:
    Push -> Read Client -> Repository Identity Check -> Commit Comparison ->
    Context Builder -> ScoutAgent -> Worthiness Policy -> Authoritative Finding Materialization ->
    Independent Verifier Context Fetch -> VerifierPipeline.run() -> Result Aggregation.
    """

    def __init__(
        self,
        scout_agent: ScoutAgent,
        verifier_pipeline: VerifierPipeline,
        read_client_factory: Callable[[int], Any],  # installation_id -> ScoutGitHubReadClient
        downstream_write_client_factory: Callable[[int], Any] | None = None,
    ) -> None:
        self.scout_agent = scout_agent
        self.verifier_pipeline = verifier_pipeline
        self.read_client_factory = read_client_factory
        self.downstream_write_client_factory = downstream_write_client_factory

    async def process_push(self, event: GitHubPushEvent) -> ScoutRunResult:
        failures: list[str] = []
        read_client: ScoutGitHubReadClient = await self.read_client_factory(event.installation_id)

        # 1. Verify repository identity against GitHub metadata (Fail closed on mismatch)
        try:
            await verify_repository_identity(
                read_client,
                event.repository_owner,
                event.repository_name,
                event.repository_id,
            )
        except Exception as exc:
            logger.error(f"Repository identity check failed: {exc}")
            return ScoutRunResult(
                repository_id=event.repository_id,
                commit_sha=event.after_sha,
                failures=[f"IDENTITY_CHECK_FAILED: {str(exc)}"],
            )

        # 2. Commit comparison / safe fallback
        is_initial_push = event.before_sha == ALL_ZERO_SHA
        is_forced_push = event.forced
        tree_truncated = False
        changed_files: list[ChangedFile] = []

        if is_initial_push:
            # Initial push: bounded tree scan at after_sha
            try:
                tree_items, tree_truncated = await read_client.get_repository_tree(
                    event.repository_owner, event.repository_name, event.after_sha
                )
                for item in tree_items:
                    if item.get("type") == "blob":
                        changed_files.append(ChangedFile(path=item["path"], status="added"))
            except Exception as exc:
                failures.append(f"INITIAL_TREE_SCAN_FAILED: {str(exc)}")
        else:
            try:
                compare_data = await read_client.compare_commits(
                    event.repository_owner,
                    event.repository_name,
                    event.before_sha,
                    event.after_sha,
                )
                for file_dict in compare_data.get("files", []):
                    changed_files.append(
                        ChangedFile(
                            path=file_dict.get("filename", ""),
                            previous_path=file_dict.get("previous_filename"),
                            status=file_dict.get("status", "modified"),
                            additions=file_dict.get("additions", 0),
                            deletions=file_dict.get("deletions", 0),
                            changes=file_dict.get("changes", 0),
                            patch=file_dict.get("patch"),
                        )
                    )
            except Exception as exc:
                if is_forced_push:
                    # Fall back to bounded tree scan on forced push
                    try:
                        tree_items, tree_truncated = await read_client.get_repository_tree(
                            event.repository_owner, event.repository_name, event.after_sha
                        )
                        for item in tree_items:
                            if item.get("type") == "blob":
                                changed_files.append(ChangedFile(path=item["path"], status="modified"))
                    except Exception as tree_exc:
                        failures.append(f"FORCED_PUSH_FALLBACK_FAILED: {str(tree_exc)}")
                else:
                    failures.append(f"COMMIT_COMPARE_FAILED: {str(exc)}")

        # 3. Build bounded ScoutContext
        context_builder = ScoutContextBuilder(read_client)
        scout_context = await context_builder.build_context(
            event=event,
            changed_files=changed_files,
            is_initial_push=is_initial_push,
            is_forced_push=is_forced_push,
            tree_truncated=tree_truncated,
        )

        # 4. Scout Agent Discovery
        try:
            scout_response = await self.scout_agent.discover(event, scout_context)
            ai_drafts = scout_response.findings
        except Exception as exc:
            logger.error(f"ScoutAgent discovery error: {exc}")
            failures.append(f"SCOUT_AGENT_FAILED: {str(exc)}")
            return ScoutRunResult(
                repository_id=event.repository_id,
                commit_sha=event.after_sha,
                changed_files=[cf.path for cf in changed_files],
                failures=failures,
            )

        # 5. Deterministic Issue-Worthiness Policy & Caps
        escalated_drafts, suppression_reasons = rank_and_filter_drafts(
            ai_drafts, scout_context
        )

        # 6. Materialize Findings & Downstream Independent Verification
        issues_created = 0
        verifier_rejected = 0

        # Downstream verifier uses either write_client or read_client
        downstream_client = (
            await self.downstream_write_client_factory(event.installation_id)
            if self.downstream_write_client_factory
            else read_client
        )

        for draft in escalated_drafts:
            # Authoritative materialization
            finding = materialize_finding(draft, event)

            # Independent targeted verifier context fetch (Never reuse ScoutContext directly)
            try:
                repo_context = await fetch_repo_context(
                    finding,
                    downstream_client,
                    event.repository_owner,
                    event.repository_name,
                )
            except Exception as exc:
                failures.append(f"VERIFIER_CONTEXT_FETCH_FAILED: {str(exc)}")
                continue

            # Run VerifierPipeline directly in-process
            try:
                res = await self.verifier_pipeline.run(
                    finding,
                    repo_context,
                    event.repository_owner,
                    event.repository_name,
                    github_write_client=downstream_client,
                )
                if res and getattr(res, "issue_number", None):
                    issues_created += 1
                else:
                    verifier_rejected += 1
            except Exception as exc:
                logger.error(f"Verifier execution error for finding {finding.finding_id}: {exc}")
                failures.append(f"VERIFIER_EXECUTION_FAILED: {str(exc)}")
                verifier_rejected += 1

        return ScoutRunResult(
            repository_id=event.repository_id,
            commit_sha=event.after_sha,
            changed_files=[cf.path for cf in changed_files],
            ai_drafts=len(ai_drafts),
            suppressed_findings=len(suppression_reasons),
            escalated_findings=len(escalated_drafts),
            verifier_rejected=verifier_rejected,
            issues_created=issues_created,
            failures=failures,
            suppression_reasons=suppression_reasons,
        )
