import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session
import models
from database import get_db


def _require_env(key: str) -> str:
    """Fail loudly at startup if a required secret is missing."""
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(
            f"Required environment variable '{key}' is not set. "
            f"Generate with: openssl rand -hex 32"
        )
    return val


SECRET_KEY    = _require_env("SECRET_KEY")
ALGORITHM     = "HS256"
ACCESS_EXPIRE = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", 480))
REFRESH_EXPIRE = int(os.environ.get("REFRESH_TOKEN_EXPIRE_DAYS", 7))

pwd_context   = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

ROLE_HIERARCHY = {"read_only": 0, "modify": 1, "admin": 2}

# Minimum password length enforced at creation/reset time
MIN_PASSWORD_LEN = 12


# ── Passwords ─────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LEN} characters")
    return pwd_context.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ── Tokens ────────────────────────────────────────────────────────────────────

def create_access_token(user_id: int, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_EXPIRE)
    return jwt.encode(
        {"sub": str(user_id), "role": role, "exp": expire, "type": "access"},
        SECRET_KEY, algorithm=ALGORITHM,
    )

def create_refresh_token(user_id: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_EXPIRE)
    return jwt.encode(
        {"sub": str(user_id), "exp": expire, "type": "refresh"},
        SECRET_KEY, algorithm=ALGORITHM,
    )

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── Current user dependency ───────────────────────────────────────────────────

def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> models.User:
    payload = decode_token(token)
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid token type")

    user = db.query(models.User).filter(
        models.User.id == int(payload["sub"]),
        models.User.is_active == True,
    ).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


def require_role(minimum_role: str):
    def checker(current_user: models.User = Depends(get_current_user)):
        if ROLE_HIERARCHY.get(current_user.role, -1) < ROLE_HIERARCHY[minimum_role]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{minimum_role}' or higher required",
            )
        return current_user
    return checker


# ── Audit helper ──────────────────────────────────────────────────────────────

def log_action(db: Session, user_id: int, action: str, resource: str,
               resource_id: Optional[int], detail: Optional[str] = None):
    entry = models.AuditLog(
        user_id=user_id,
        action=action,
        resource=resource,
        resource_id=resource_id,
        detail=detail,
    )
    db.add(entry)
    db.commit()
