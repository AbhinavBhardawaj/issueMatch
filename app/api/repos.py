"""
Per-repo dashboard endpoints.

GET   /api/repos/{owner}/{repo}/scout/enabled   — read scout toggle state
PATCH /api/repos/{owner}/{repo}/scout/enabled   — set scout toggle state
GET   /api/repos/{owner}/{repo}/scout/findings  — findings from issue_write_intents
GET   /api/repos/{owner}/{repo}/assignments      — open GitHub issues
"""

import datetime
import logging
import time

import httpx
from fastapi import APIRouter, Cookie, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.auth import _get_session_user
from app.storage.sqlite import get_db_connection, get_default_db_path, init_schema

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db_path() -> str:
    return get_default_db_path()


async def _ensure_config_table() -> None:
    """Create repo_scout_config table if it doesn't exist yet."""
    async with await get_db_connection(_db_path()) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS repo_scout_config (
                owner      TEXT NOT NULL,
                repo_name  TEXT NOT NULL,
                enabled    INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL,
                PRIMARY KEY (owner, repo_name)
            );
            """
        )
        await db.commit()


def _require_user(im_session: str | None) -> dict:
    user = _get_session_user(im_session)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def _time_ago(ts: float) -> str:
    diff = int(time.time() - ts)
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{diff // 60}m ago"
    if diff < 86400:
        return f"{diff // 3600}h ago"
    return f"{diff // 86400}d ago"


# ---------------------------------------------------------------------------
# Scout toggle
# ---------------------------------------------------------------------------

class ScoutToggle(BaseModel):
    enabled: bool


@router.get("/repos/{owner}/{repo}/scout/enabled")
async def get_scout_enabled(
    owner: str,
    repo: str,
    im_session: str | None = Cookie(default=None),
) -> JSONResponse:
    _require_user(im_session)
    await _ensure_config_table()

    async with await get_db_connection(_db_path()) as db:
        async with db.execute(
            "SELECT enabled FROM repo_scout_config WHERE owner = ? AND repo_name = ?;",
            (owner, repo),
        ) as cur:
            row = await cur.fetchone()

    enabled = bool(row["enabled"]) if row else True
    return JSONResponse({"enabled": enabled})


@router.patch("/repos/{owner}/{repo}/scout/enabled")
async def set_scout_enabled(
    owner: str,
    repo: str,
    body: ScoutToggle,
    im_session: str | None = Cookie(default=None),
) -> JSONResponse:
    _require_user(im_session)
    await _ensure_config_table()

    now = time.time()
    async with await get_db_connection(_db_path()) as db:
        await db.execute(
            """
            INSERT INTO repo_scout_config (owner, repo_name, enabled, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(owner, repo_name) DO UPDATE
            SET enabled = excluded.enabled, updated_at = excluded.updated_at;
            """,
            (owner, repo, int(body.enabled), now),
        )
        await db.commit()

    return JSONResponse({"enabled": body.enabled})


# ---------------------------------------------------------------------------
# Findings — from issue_write_intents table
# ---------------------------------------------------------------------------

@router.get("/repos/{owner}/{repo}/scout/findings")
async def get_findings(
    owner: str,
    repo: str,
    im_session: str | None = Cookie(default=None),
) -> JSONResponse:
    user = _require_user(im_session)
    access_token = user.get("access_token")

    db_path = _db_path()
    await init_schema(db_path)

    async with await get_db_connection(db_path) as db:
        async with db.execute(
            """
            SELECT intent_id, commit_sha, issue_number, state, updated_at
            FROM issue_write_intents
            WHERE owner = ? AND repo_name = ?
            ORDER BY updated_at DESC
            LIMIT 50;
            """,
            (owner, repo),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    if not rows:
        return JSONResponse([])

    # For committed findings, fetch the real issue title from GitHub
    issue_titles: dict[int, str] = {}
    committed = [r for r in rows if r["issue_number"] and r["state"] == "COMMITTED"]

    if committed and access_token:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                for r in committed[:20]:
                    num = r["issue_number"]
                    resp = await client.get(
                        f"https://api.github.com/repos/{owner}/{repo}/issues/{num}",
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "Accept": "application/vnd.github+json",
                            "X-GitHub-Api-Version": "2022-11-28",
                        },
                    )
                    if resp.status_code == 200:
                        issue_titles[num] = resp.json().get("title", f"Issue #{num}")
        except Exception as exc:
            logger.warning("Failed to fetch issue titles from GitHub: %s", exc)

    state_map = {
        "COMMITTED": "verified",
        "POSTING":   "verifying",
        "PREPARED":  "proposed",
        "ABORTED":   "rejected",
    }

    findings = []
    for r in rows:
        num = r["issue_number"]
        status = state_map.get(r["state"], "proposed")
        title = (
            issue_titles.get(num, f"Issue #{num}")
            if num
            else f"Finding {r['intent_id'][:8]}"
        )
        findings.append({
            "id":       r["intent_id"],
            "commit":   (r["commit_sha"] or "")[:7],
            "title":    title,
            "status":   status,
            "issueUrl": f"https://github.com/{owner}/{repo}/issues/{num}" if num else None,
            "time":     _time_ago(r["updated_at"]),
        })

    return JSONResponse(findings)


# ---------------------------------------------------------------------------
# Assignments — open GitHub issues for this repo
# ---------------------------------------------------------------------------

@router.get("/repos/{owner}/{repo}/assignments")
async def get_assignments(
    owner: str,
    repo: str,
    im_session: str | None = Cookie(default=None),
) -> JSONResponse:
    user = _require_user(im_session)
    access_token = user.get("access_token")

    if not access_token:
        raise HTTPException(status_code=401, detail="No access token")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/issues",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                params={
                    "state": "open",
                    "per_page": 20,
                    "sort": "updated",
                    "direction": "desc",
                },
            )

        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail="Repo not found or no access")

        if resp.status_code != 200:
            logger.warning("GitHub issues fetch returned %s: %s", resp.status_code, resp.text)
            return JSONResponse([])

        # Filter out pull requests (GitHub returns PRs in /issues)
        issues = [i for i in resp.json() if "pull_request" not in i]

        assignments = []
        for issue in issues[:15]:
            assignees = issue.get("assignees") or []
            if assignees:
                candidate = "@" + assignees[0]["login"]
                outcome = "accepted"
            elif issue.get("assignee"):
                candidate = "@" + issue["assignee"]["login"]
                outcome = "accepted"
            else:
                candidate = "unassigned"
                outcome = "waiting"

            updated = issue.get("updated_at", "")
            try:
                dt = datetime.datetime.fromisoformat(updated.replace("Z", "+00:00"))
                time_str = _time_ago(dt.timestamp())
            except Exception:
                time_str = "recently"

            body = issue.get("body") or "No description provided."
            assignments.append({
                "id":        str(issue["number"]),
                "issue":     f"#{issue['number']} — {issue['title']}",
                "candidate": candidate,
                "outcome":   outcome,
                "feedback":  body[:120] + ("…" if len(body) > 120 else ""),
                "time":      time_str,
            })

        return JSONResponse(assignments)

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to fetch assignments: %s", exc)
        return JSONResponse([])
