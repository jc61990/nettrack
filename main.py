import os
from fastapi import FastAPI, HTTPException, Depends, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from contextlib import asynccontextmanager
from sqlalchemy.orm import Session
from typing import List, Optional
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import models, schemas, auth
from database import engine, get_db
from routers import users, audit
from routers import scanner as scanner_router
from routers import schedule as schedule_router
from routers import floors as floors_router
from routers import queue as queue_router
from routers import vlans as vlans_router
import oidc
from scanner.scheduler import start_scheduler, stop_scheduler



# ── Security headers middleware ───────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"]  = "nosniff"
        response.headers["X-Frame-Options"]          = "DENY"
        response.headers["X-XSS-Protection"]         = "1; mode=block"
        response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"]        = "geolocation=(), microphone=(), camera=()"
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        response.headers["Content-Security-Policy"]   = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'"
        )
        return response


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield
    stop_scheduler()


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="NetTrack API",
    version="1.0.0",
    lifespan=lifespan,
    # Hide API docs in production
    docs_url="/api/docs" if os.environ.get("ENVIRONMENT") != "production" else None,
    redoc_url=None,
)


# Trusted hosts — prevents host header injection
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

# Security headers on every response
app.add_middleware(SecurityHeadersMiddleware)

# CORS — locked to the frontend origin, never wildcard
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000").split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(users.router)
app.include_router(audit.router)
app.include_router(oidc.router)
app.include_router(scanner_router.router)
app.include_router(schedule_router.router)
app.include_router(floors_router.router)
app.include_router(queue_router.router)
app.include_router(vlans_router.router)


# ── Devices ───────────────────────────────────────────────────────────────────

@app.get("/api/devices", response_model=List[schemas.DeviceOut])
def list_devices(
    search: Optional[str] = Query(None, max_length=100),
    type:   Optional[str] = Query(None, max_length=64),
    status: Optional[str] = Query(None, max_length=32),
    switch: Optional[str] = Query(None, max_length=64),
    floor:  Optional[str] = Query(None, max_length=16),
    _: models.User = Depends(auth.require_role("read_only")),
    db: Session = Depends(get_db),
):
    q = db.query(models.Device)
    if search:
        like = f"%{search}%"
        q = q.filter(
            models.Device.hostname.ilike(like)
            | models.Device.ip.ilike(like)
            | models.Device.mac.ilike(like)
            | models.Device.location.ilike(like)
        )
    if type:   q = q.filter(models.Device.type == type)
    if status: q = q.filter(models.Device.status == status)
    if switch: q = q.filter(models.Device.switch == switch)
    if floor:  q = q.filter(models.Device.floor == floor)
    return q.order_by(models.Device.hostname).all()


@app.post("/api/devices", response_model=schemas.DeviceOut, status_code=201)
def create_device(
    device: schemas.DeviceCreate,
    current_user: models.User = Depends(auth.require_role("modify")),
    db: Session = Depends(get_db),
):
    db_device = models.Device(**device.model_dump())
    db.add(db_device)
    db.commit()
    db.refresh(db_device)
    auth.log_action(db, current_user.id, "create", "device", db_device.id, db_device.hostname)
    return db_device


@app.get("/api/devices/{device_id}", response_model=schemas.DeviceOut)
def get_device(
    device_id: int,
    _: models.User = Depends(auth.require_role("read_only")),
    db: Session = Depends(get_db),
):
    device = db.query(models.Device).filter(models.Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


@app.put("/api/devices/{device_id}", response_model=schemas.DeviceOut)
def update_device(
    device_id: int,
    updates: schemas.DeviceUpdate,
    current_user: models.User = Depends(auth.require_role("modify")),
    db: Session = Depends(get_db),
):
    device = db.query(models.Device).filter(models.Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    for field, value in updates.model_dump(exclude_unset=True).items():
        setattr(device, field, value)
    db.commit()
    db.refresh(device)
    auth.log_action(db, current_user.id, "update", "device", device_id, device.hostname)
    return device


@app.delete("/api/devices/{device_id}", status_code=204)
def delete_device(
    device_id: int,
    current_user: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    device = db.query(models.Device).filter(models.Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    auth.log_action(db, current_user.id, "delete", "device", device_id, device.hostname)
    db.delete(device)
    db.commit()


@app.post("/api/devices/bulk-upsert", response_model=schemas.BulkUpsertResult)
def bulk_upsert(
    payload: schemas.BulkUpsertRequest,
    current_user: models.User = Depends(auth.require_role("modify")),
    db: Session = Depends(get_db),
):
    created, updated = 0, 0
    for item in payload.devices:
        existing = db.query(models.Device).filter(models.Device.mac == item.mac).first()
        if existing:
            for field, value in item.model_dump(exclude_unset=True).items():
                setattr(existing, field, value)
            updated += 1
        else:
            db.add(models.Device(**item.model_dump()))
            created += 1
    db.commit()
    auth.log_action(db, current_user.id, "bulk_upsert", "device", None,
                    f"created={created} updated={updated}")
    return {"created": created, "updated": updated}


# ── Switches ──────────────────────────────────────────────────────────────────

@app.get("/api/switches", response_model=List[schemas.SwitchOut])
def list_switches(
    _: models.User = Depends(auth.require_role("read_only")),
    db: Session = Depends(get_db),
):
    return db.query(models.Switch).order_by(models.Switch.name).all()


@app.post("/api/switches", response_model=schemas.SwitchOut, status_code=201)
def create_switch(
    switch: schemas.SwitchCreate,
    current_user: models.User = Depends(auth.require_role("modify")),
    db: Session = Depends(get_db),
):
    db_switch = models.Switch(**switch.model_dump())
    db.add(db_switch)
    db.commit()
    db.refresh(db_switch)
    auth.log_action(db, current_user.id, "create", "switch", db_switch.id, db_switch.name)
    return db_switch


@app.put("/api/switches/{switch_id}", response_model=schemas.SwitchOut)
def update_switch(
    switch_id: int,
    updates: schemas.SwitchCreate,
    current_user: models.User = Depends(auth.require_role("modify")),
    db: Session = Depends(get_db),
):
    switch = db.query(models.Switch).filter(models.Switch.id == switch_id).first()
    if not switch:
        raise HTTPException(status_code=404, detail="Switch not found")
    for field, value in updates.model_dump().items():
        setattr(switch, field, value)
    db.commit()
    db.refresh(switch)
    auth.log_action(db, current_user.id, "update", "switch", switch_id, switch.name)
    return switch


@app.delete("/api/switches/{switch_id}", status_code=204)
def delete_switch(
    switch_id: int,
    current_user: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    switch = db.query(models.Switch).filter(models.Switch.id == switch_id).first()
    if not switch:
        raise HTTPException(status_code=404, detail="Switch not found")
    auth.log_action(db, current_user.id, "delete", "switch", switch_id, switch.name)
    db.delete(switch)
    db.commit()


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok"}
