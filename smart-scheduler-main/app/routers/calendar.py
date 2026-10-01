"""Connect a Google Calendar from the browser.

Two ways in, both tied to the visitor's browser cookie so people never affect each other's sessions:
- "Sign in with Google": the standard OAuth web flow (start -> Google consent -> callback).
- Upload the token.json produced by scripts/authorize_google.py (for the deployment owner / offline use).
Disconnecting reverts the visitor to the deployment's default calendar, if one is configured.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from google_auth_oauthlib.flow import Flow

from app.calendar.google_calendar import SCOPES
from app.web import UID_COOKIE, browser_uid

if TYPE_CHECKING:  # avoids a circular import: main.py includes this router
    from app.main import Runtime

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/calendar")

# OAuth `state` values we issued -> (browser uid, created_at, PKCE code_verifier, redirect_uri).
# The verifier and redirect URI must be reused verbatim in the token exchange, or Google answers invalid_grant.
_pending_states: dict[str, tuple[str, float, str, str]] = {}
STATE_TTL_S = 600


def _require_uid(request: Request) -> str:
    uid = browser_uid(request.cookies)
    if not uid:
        raise HTTPException(400, f"missing {UID_COOKIE} cookie; open the app root page first")
    return uid


def _redirect_uri(request: Request, rt: Runtime) -> str:
    base = rt.settings.public_base_url.rstrip("/") or str(request.base_url).rstrip("/")
    return f"{base}/api/calendar/oauth/callback"


def _flow(rt: Runtime, redirect_uri: str, code_verifier: str | None = None) -> Flow:
    client = rt.oauth_client_config()
    if client is None:
        raise HTTPException(503, "Google sign-in is not configured on this server (GOOGLE_OAUTH_CLIENT_JSON)")
    config = {
        "web": {
            **client,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    if code_verifier:  # callback: reuse the verifier that produced the code_challenge in the consent URL
        return Flow.from_client_config(
            config,
            scopes=SCOPES,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
            autogenerate_code_verifier=False,
        )
    return Flow.from_client_config(config, scopes=SCOPES, redirect_uri=redirect_uri)


@router.get("/status")
async def status(request: Request):
    rt: Runtime = request.app.state.rt
    return rt.calendar_status(browser_uid(request.cookies))


@router.get("/oauth/start")
async def oauth_start(request: Request):
    """Send the visitor to Google's consent screen."""
    rt: Runtime = request.app.state.rt
    uid = _require_uid(request)
    redirect_uri = _redirect_uri(request, rt)
    flow = _flow(rt, redirect_uri)
    state = secrets.token_urlsafe(24)
    now = time.time()
    for key, entry in list(_pending_states.items()):  # expire stale states
        if now - entry[1] > STATE_TTL_S:
            _pending_states.pop(key, None)
    # offline + consent: guarantees a refresh token so the calendar keeps working after the hour-long access token.
    url, _ = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true", state=state)
    _pending_states[state] = (uid, now, flow.code_verifier, redirect_uri)  # verifier exists once the URL is built
    return RedirectResponse(url, status_code=302)


@router.get("/oauth/callback")
async def oauth_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    """Google redirects here; exchange the code and attach the calendar to the visitor's browser."""
    rt: Runtime = request.app.state.rt
    entry = _pending_states.pop(state, None)
    if error or entry is None or not code:
        return RedirectResponse(f"/?calendar=error&reason={error or 'invalid_state'}", status_code=302)
    uid, created_at, code_verifier, redirect_uri = entry
    if time.time() - created_at > STATE_TTL_S:
        return RedirectResponse("/?calendar=error&reason=expired_state", status_code=302)
    if uid != browser_uid(request.cookies):
        return RedirectResponse("/?calendar=error&reason=cookie_mismatch", status_code=302)
    try:
        flow = _flow(rt, redirect_uri, code_verifier)
        await asyncio.to_thread(flow.fetch_token, code=code)
        await rt.connect_user_calendar(uid, flow.credentials.to_json())
    except Exception as exc:
        log.warning("Google sign-in failed at token exchange: %s: %s", type(exc).__name__, exc)
        return RedirectResponse(f"/?calendar=error&reason={type(exc).__name__}", status_code=302)
    return RedirectResponse("/?calendar=connected", status_code=302)


@router.post("/token")
async def upload_token(request: Request, file: Annotated[UploadFile, File()]):
    """Alternative to sign-in: upload the token.json written by scripts/authorize_google.py."""
    rt: Runtime = request.app.state.rt
    uid = _require_uid(request)
    raw = (await file.read()).decode("utf-8", errors="replace")
    try:
        return await rt.connect_user_calendar(uid, raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # Google rejected the token (revoked, wrong project, expired test-user grant)
        raise HTTPException(400, f"Google rejected the token: {type(exc).__name__}: {str(exc)[:200]}") from exc


@router.delete("/token")
async def disconnect(request: Request):
    rt: Runtime = request.app.state.rt
    return rt.disconnect_user_calendar(_require_uid(request))
