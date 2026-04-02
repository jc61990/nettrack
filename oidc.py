"""
OIDC SSO integration — works with Azure AD, Okta, Google Workspace, or any
provider that exposes a standard /.well-known/openid-configuration endpoint.

Tokens are delivered via HttpOnly cookies, never via URL query strings.

Environment variables required for SSO:
    OIDC_CLIENT_ID       Your app's client ID from the identity provider
    OIDC_CLIENT_SECRET   Your app's client secret
    OIDC_DISCOVERY_URL   e.g. https://login.microsoftonline.com/{tenant}/v2.0
    OIDC_REDIRECT_URI    e.g. https://nettrack.yourcompany.com/api/auth/callback
    OIDC_DEFAULT_ROLE    Role to assign new SSO users (default: read_only)
    FRONTEND_URL         Where to redirect after successful SSO login
"""

import hashlib
import hmac
import os
import secrets

import httpx
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from urllib.parse import urlencode

import auth
import models
from database import get_db

router = APIRouter(prefix="/api/auth", tags=["SSO"])

OIDC_CLIENT_ID     = os.environ.get("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
OIDC_DISCOVERY_URL = os.environ.get("OIDC_DISCOVERY_URL", "")
OIDC_REDIRECT_URI  = os.environ.get("OIDC_REDIRECT_URI", "")
OIDC_DEFAULT_ROLE  = os.environ.get("OIDC_DEFAULT_ROLE", "read_only")
FRONTEND_URL       = os.environ.get("FRONTEND_URL", "http://localhost:3000")

# HMAC key for signing state tokens — prevents CSRF on the callback
_STATE_HMAC_KEY = auth.SECRET_KEY.encode()

# In-memory nonce store — swap for Redis in multi-instance deployments
_state_store: dict[str, bool] = {}


def _make_state() -> str:
    nonce = secrets.token_urlsafe(32)
    sig   = hmac.new(_STATE_HMAC_KEY, nonce.encode(), hashlib.sha256).hexdigest()
    token = f"{nonce}.{sig}"
    _state_store[token] = True
    return token


def _verify_state(token: str) -> bool:
    if token not in _state_store:
        return False
    del _state_store[token]
    try:
        nonce, sig = token.rsplit(".", 1)
        expected   = hmac.new(_STATE_HMAC_KEY, nonce.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


async def _get_oidc_config() -> dict:
    if not OIDC_DISCOVERY_URL:
        raise HTTPException(status_code=501, detail="SSO is not configured")
    url = OIDC_DISCOVERY_URL.rstrip("/") + "/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


@router.get("/sso/login")
async def sso_login():
    """Redirect browser to the identity provider login page."""
    config = await _get_oidc_config()
    state  = _make_state()
    params = urlencode({
        "client_id":     OIDC_CLIENT_ID,
        "response_type": "code",
        "scope":         "openid email profile",
        "redirect_uri":  OIDC_REDIRECT_URI,
        "state":         state,
    })
    return RedirectResponse(f"{config['authorization_endpoint']}?{params}")


@router.get("/callback")
async def sso_callback(code: str, state: str, db: Session = Depends(get_db)):
    """
    Handle the OIDC authorization code callback.
    Delivers tokens as HttpOnly cookies — nothing lands in the URL.
    """
    if not _verify_state(state):
        raise HTTPException(status_code=400, detail="Invalid or expired state parameter")

    config = await _get_oidc_config()

    async with httpx.AsyncClient(timeout=10) as client:
        # Exchange code for tokens
        token_resp = await client.post(
            config["token_endpoint"],
            data={
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  OIDC_REDIRECT_URI,
                "client_id":     OIDC_CLIENT_ID,
                "client_secret": OIDC_CLIENT_SECRET,
            },
        )
        token_resp.raise_for_status()
        provider_tokens = token_resp.json()

        # Get user info
        userinfo_resp = await client.get(
            config["userinfo_endpoint"],
            headers={"Authorization": f"Bearer {provider_tokens['access_token']}"},
        )
        userinfo_resp.raise_for_status()
        userinfo = userinfo_resp.json()

    email = userinfo.get("email", "").lower().strip()
    if not email:
        raise HTTPException(status_code=400, detail="No email returned from SSO provider")

    # Find or provision user
    user = db.query(models.User).filter(models.User.email == email).first()
    if not user:
        user = models.User(
            email=email,
            full_name=userinfo.get("name", email),
            role=OIDC_DEFAULT_ROLE,
            auth_provider="oidc",
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")

    access_token  = auth.create_access_token(user.id, user.role)
    refresh_token = auth.create_refresh_token(user.id)
    user.refresh_token = refresh_token
    db.commit()

    # Redirect to frontend — access token in HttpOnly cookie, NOT the URL
    response = RedirectResponse(url=f"{FRONTEND_URL}/")
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=False,
        samesite="lax",
        max_age=60 * auth.ACCESS_EXPIRE,
        path="/",
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=False,
        samesite="lax",
        max_age=60 * 60 * 24 * auth.REFRESH_EXPIRE,
        path="/api/auth/refresh",
    )
    return response
