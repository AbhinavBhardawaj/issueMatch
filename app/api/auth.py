"""
GitHub OAuth login flow for the IssueMatch frontend.

Endpoints:
  GET  /api/auth/github    — start OAuth, redirect user to GitHub
  GET  /api/auth/callback  — GitHub redirects here with ?code=
  GET  /api/auth/me        — return current session user (or 401)
  POST /api/auth/logout    — clear session cookie
  GET  /api/repos          — list repos where the GitHub App is installed for this user
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
from typing import Any

import httpx
from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _require_env(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return val


def get_serializer() -> URLSafeTimedSerializer:
    secret = os.getenv("SESSION_SECRET_KEY", "dev-secret-change-in-production")
    return URLSafeTimedSerializer(secret, salt="issuematch-session")


GITHUB_OAUTH_CLIENT_ID     = lambda: os.getenv("GITHUB_OAUTH_CLIENT_ID", "")
GITHUB_OAUTH_CLIENT_SECRET = lambda: os.getenv("GITHUB_OAUTH_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI         = lambda: os.getenv("OAUTH_REDIRECT_URI", "http://localhost:8000/api/auth/callback")
FRONTEND_URL               = lambda: os.getenv("FRONTEND_URL", "http://localhost:5173")

COOKIE_NAME    = "im_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 days


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def _make_session_cookie(data: dict) -> str:
    return get_serializer().dumps(data)


def _read_session_cookie(token: str) -> dict | None:
    try:
        return get_serializer().loads(token, max_age=COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def _set_session(response: JSONResponse | RedirectResponse, data: dict) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=_make_session_cookie(data),
        httponly=True,
        samesite="lax",
        secure=False,      # set True in production behind HTTPS
        max_age=COOKIE_MAX_AGE,
        path="/",
    )


def _clear_session(response: JSONResponse | RedirectResponse) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=False)


def _get_session_user(im_session: str | None) -> dict | None:
    if not im_session:
        return None
    return _read_session_cookie(im_session)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class UserOut(BaseModel):
    login: str
    name: str | None = None
    avatar: str | None = None
    github_id: int


class RepoOut(BaseModel):
    owner: str
    repo: str
    scout: bool = False
    lastPush: str = "unknown"
    findings: int = 0


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/auth/github")
async def github_login() -> RedirectResponse:
    """Redirect the user to GitHub to authorise the OAuth App."""
    state = secrets.token_urlsafe(16)
    params = (
        f"client_id={GITHUB_OAUTH_CLIENT_ID()}"
        f"&redirect_uri={OAUTH_REDIRECT_URI()}"
        f"&scope=repo,read:user"
        f"&prompt=consent"
        f"&state={state}"
    )
    url = f"https://github.com/login/oauth/authorize?{params}"
    return RedirectResponse(url=url, status_code=302)


@router.get("/auth/callback")
async def github_callback(code: str, state: str | None = None) -> RedirectResponse:
    """
    GitHub redirects here after the user approves.
    Exchange the code for an access token, fetch the user profile,
    create a signed session cookie, redirect to the frontend dashboard.
    """
    frontend = FRONTEND_URL()

    async with httpx.AsyncClient() as client:
        # Exchange code for access token
        token_resp = await client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id":     GITHUB_OAUTH_CLIENT_ID(),
                "client_secret": GITHUB_OAUTH_CLIENT_SECRET(),
                "code":          code,
                "redirect_uri":  OAUTH_REDIRECT_URI(),
            },
        )

    if token_resp.status_code != 200:
        logger.error("GitHub token exchange failed: %s", token_resp.text)
        return RedirectResponse(url=f"{frontend}/login?error=token_exchange_failed")

    token_data = token_resp.json()
    access_token = token_data.get("access_token")

    if not access_token:
        logger.error("No access_token in GitHub response: %s", token_data)
        return RedirectResponse(url=f"{frontend}/login?error=no_token")

    # Fetch the GitHub user profile
    async with httpx.AsyncClient() as client:
        user_resp = await client.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept":        "application/vnd.github+json",
            },
        )

    if user_resp.status_code != 200:
        logger.error("GitHub user fetch failed: %s", user_resp.text)
        return RedirectResponse(url=f"{frontend}/login?error=user_fetch_failed")

    gh_user = user_resp.json()

    session_data = {
        "login":        gh_user["login"],
        "name":         gh_user.get("name"),
        "avatar":       gh_user.get("avatar_url"),
        "github_id":    gh_user["id"],
        "access_token": access_token,
    }

    redirect = RedirectResponse(url=f"{frontend}/dashboard", status_code=302)
    _set_session(redirect, session_data)
    return redirect


@router.get("/auth/me", response_model=UserOut)
async def get_me(im_session: str | None = Cookie(default=None)) -> UserOut:
    """Return the currently logged-in user, or 401."""
    user = _get_session_user(im_session)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return UserOut(
        login=user["login"],
        name=user.get("name"),
        avatar=user.get("avatar"),
        github_id=user["github_id"],
    )


@router.post("/auth/logout")
async def logout(im_session: str | None = Cookie(default=None)) -> JSONResponse:
    """Clear the session cookie."""
    response = JSONResponse(content={"ok": True})
    _clear_session(response)
    return response


@router.get("/repos", response_model=list[RepoOut])
async def list_repos(im_session: str | None = Cookie(default=None)) -> list[RepoOut]:
    user = _get_session_user(im_session)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    access_token = user.get("access_token")
    if not access_token:
        raise HTTPException(status_code=401, detail="No access token in session")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.github.com/user/repos",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                params={
                    "sort": "pushed",
                    "per_page": 50,
                    "affiliation": "owner,collaborator",
                },
            )

        if resp.status_code != 200:
            logger.warning("repos fetch returned %s: %s", resp.status_code, resp.text)
            return []

        repos: list[RepoOut] = []
        for repo in resp.json():
            owner, name = repo["full_name"].split("/", 1)
            pushed = repo.get("pushed_at", "")
            repos.append(RepoOut(
                owner=owner,
                repo=name,
                scout=False,
                lastPush=pushed[:10] if pushed else "unknown",
                findings=0,
            ))
        return repos

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to fetch repos: %s", exc)
        return []
