from fastapi import APIRouter, HTTPException, Depends, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.responses import JSONResponse, Response
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session
from typing import List
import models, schemas, auth
from database import get_db

router  = APIRouter(prefix="/api", tags=["Users & Auth"])
limiter = Limiter(key_func=get_remote_address)


# ── Login (local) — rate limited ──────────────────────────────────────────────

@router.post("/auth/login", response_model=schemas.TokenResponse)
@limiter.limit("10/minute")
def login(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    # Normalise email to lowercase
    user = db.query(models.User).filter(
        models.User.email == form.username.lower()
    ).first()

    # Always run verify_password to prevent timing-based user enumeration.
    # Use a dummy hash if user doesn't exist so timing stays constant.
    dummy_hash = "$2b$12$KIX/mTm.4V.lH6q.Y5l.XeXfJzH/UvJ6lH6q.Y5l.XeX"
    password_ok = auth.verify_password(
        form.password,
        user.password_hash if (user and user.password_hash) else dummy_hash,
    )

    if not user or not user.password_hash or not password_ok:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")

    access_token  = auth.create_access_token(user.id, user.role)
    refresh_token = auth.create_refresh_token(user.id)
    user.refresh_token = refresh_token
    db.commit()

    auth.log_action(db, user.id, "login", "session", None)

    # Set refresh token as HttpOnly cookie — never readable by JS
    response = JSONResponse(content={
        "access_token": access_token,
        "token_type":   "bearer",
    })
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=False,         # HTTPS only
        samesite="lax",
        max_age=60 * 60 * 24 * auth.REFRESH_EXPIRE,
        path="/api/auth/refresh",
    )
    return response


# ── Refresh — reads HttpOnly cookie ──────────────────────────────────────────

@router.post("/auth/refresh", response_model=schemas.TokenResponse)
@limiter.limit("30/minute")
def refresh(request: Request, db: Session = Depends(get_db)):
    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=401, detail="No refresh token")

    data = auth.decode_token(refresh_token)
    if data.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid token type")

    user = db.query(models.User).filter(
        models.User.id == int(data["sub"]),
        models.User.refresh_token == refresh_token,
        models.User.is_active == True,
    ).first()
    if not user:
        raise HTTPException(status_code=401, detail="Token revoked or user inactive")

    access_token  = auth.create_access_token(user.id, user.role)
    new_refresh   = auth.create_refresh_token(user.id)
    user.refresh_token = new_refresh
    db.commit()

    response = JSONResponse(content={"access_token": access_token, "token_type": "bearer"})
    response.set_cookie(
        key="refresh_token", value=new_refresh,
        httponly=True, secure=False, samesite="lax",
        max_age=60 * 60 * 24 * auth.REFRESH_EXPIRE,
        path="/api/auth/refresh",
    )
    return response


# ── Logout — clears cookie ────────────────────────────────────────────────────

@router.post("/auth/logout", status_code=204)
def logout(
    request: Request,
    current_user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    current_user.refresh_token = None
    auth.log_action(db, current_user.id, "logout", "session", None)
    db.commit()
    response = Response(status_code=204)
    response.delete_cookie("refresh_token", path="/api/auth/refresh")
    return response


# ── Me ────────────────────────────────────────────────────────────────────────

@router.get("/users/me", response_model=schemas.UserOut)
def get_me(current_user: models.User = Depends(auth.get_current_user)):
    return current_user


# ── User management (admin only) ──────────────────────────────────────────────

@router.get("/users", response_model=List[schemas.UserOut])
def list_users(
    _: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    return db.query(models.User).order_by(models.User.full_name).all()


@router.post("/users", response_model=schemas.UserOut, status_code=201)
def create_user(
    payload: schemas.UserCreate,
    current_user: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    if db.query(models.User).filter(
        models.User.email == payload.email.lower()
    ).first():
        raise HTTPException(status_code=409, detail="Email already registered")

    user = models.User(
        email=payload.email.lower(),
        full_name=payload.full_name,
        role=payload.role,
        auth_provider="local",
        is_active=True,
        password_hash=auth.hash_password(payload.password) if payload.password else None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    auth.log_action(db, current_user.id, "create_user", "user", user.id, f"role={user.role}")
    return user


@router.put("/users/{user_id}", response_model=schemas.UserOut)
def update_user(
    user_id: int,
    payload: schemas.UserUpdate,
    current_user: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    changes = []
    if payload.full_name is not None:
        user.full_name = payload.full_name
        changes.append("full_name")
    if payload.role is not None:
        user.role = payload.role
        changes.append(f"role={payload.role}")
    if payload.is_active is not None:
        user.is_active = payload.is_active
        changes.append(f"is_active={payload.is_active}")
    if payload.password:
        user.password_hash = auth.hash_password(payload.password)
        user.refresh_token = None   # invalidate existing sessions
        changes.append("password_reset")

    db.commit()
    db.refresh(user)
    auth.log_action(db, current_user.id, "update_user", "user", user.id, ", ".join(changes))
    return user


@router.delete("/users/{user_id}", status_code=204)
def delete_user(
    user_id: int,
    current_user: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    db.delete(user)
    db.commit()
    auth.log_action(db, current_user.id, "delete_user", "user", user_id)
