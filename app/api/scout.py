import os
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, ConfigDict
from app.github.events import GitHubPushEvent
from app.scout.service import ScoutService
from app.scout.models import ScoutRunResult

router = APIRouter()


class ScoutDevRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    installation_id: int
    repository_id: int
    owner: str
    repo_name: str
    before_sha: str
    after_sha: str
    default_branch: str = "main"


def get_scout_service() -> ScoutService:
    from app.main import get_application_scout_service
    return get_application_scout_service()


@router.post("/scout", response_model=ScoutRunResult)
async def dev_scout_endpoint(
    request: ScoutDevRequest,
    service: ScoutService = Depends(get_scout_service),
) -> ScoutRunResult:
    """
    Local development endpoint for Scout.
    Protected by ENABLE_DEV_SCOUT_API environment variable (default: false).
    Accepts only repository coordinates; server fetches all context itself.
    """
    if os.getenv("ENABLE_DEV_SCOUT_API", "").lower() not in ("true", "1", "yes"):
        raise HTTPException(status_code=404, detail="Dev scout endpoint is disabled")

    event = GitHubPushEvent(
        delivery_id="dev-scout-call",
        installation_id=request.installation_id,
        repository_id=request.repository_id,
        repository_owner=request.owner,
        repository_name=request.repo_name,
        default_branch=request.default_branch,
        before_sha=request.before_sha,
        after_sha=request.after_sha,
        ref=f"refs/heads/{request.default_branch}",
        forced=False,
        deleted=False,
    )

    return await service.process_push(event)
