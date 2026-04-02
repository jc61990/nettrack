"""
Schedule management endpoints.

GET  /api/scan/schedule          — current config, next run time, last run stats
PUT  /api/scan/schedule          — update preset / enabled, applies live (no restart)
POST /api/scan/schedule/trigger  — run immediately (alias for /api/scan/trigger)
GET  /api/scan/schedule/alerts   — email alert config
PUT  /api/scan/schedule/alerts   — update alert config
POST /api/scan/schedule/alerts/test — send a test email
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

import auth
import models
from database import get_db
from scanner.alerts import encrypt_password

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/scan/schedule", tags=["Schedule"])

# Preset → cron expression mapping
PRESETS = {
    "hourly":    "0 * * * *",
    "every_2h":  "0 */2 * * *",
    "every_4h":  "0 */4 * * *",
    "daily":     "0 2 * * *",      # 2am daily
}
PRESET_LABELS = {
    "hourly":    "Every hour",
    "every_2h":  "Every 2 hours",
    "every_4h":  "Every 4 hours",
    "daily":     "Daily at 2am",
}


def _get_or_create_schedule(db: Session) -> models.ScanSchedule:
    row = db.query(models.ScanSchedule).filter(models.ScanSchedule.id == 1).first()
    if not row:
        row = models.ScanSchedule(id=1, enabled=False, preset="every_4h")
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _get_or_create_alert(db: Session) -> models.AlertConfig:
    row = db.query(models.AlertConfig).filter(models.AlertConfig.id == 1).first()
    if not row:
        row = models.AlertConfig(id=1, enabled=False)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _next_run_time() -> Optional[str]:
    """Get the next scheduled run time from APScheduler."""
    try:
        from scanner.scheduler import _scheduler
        if _scheduler and _scheduler.running:
            job = _scheduler.get_job("network_scan")
            if job and job.next_run_time:
                return job.next_run_time.isoformat()
    except Exception:
        pass
    return None


# ── Schedule ──────────────────────────────────────────────────────────────────

class ScheduleOut(BaseModel):
    enabled:          bool
    preset:           str
    preset_label:     str
    cron:             str
    next_run_at:      Optional[str] = None
    last_run_at:      Optional[datetime] = None
    last_run_status:  Optional[str] = None
    last_run_found:   Optional[int] = None
    last_run_new:     Optional[int] = None
    last_run_offline: Optional[int] = None
    presets:          dict = {}

    class Config:
        from_attributes = True


class ScheduleUpdate(BaseModel):
    enabled: bool
    preset:  str


@router.get("", response_model=ScheduleOut)
def get_schedule(
    _: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    row = _get_or_create_schedule(db)
    return ScheduleOut(
        enabled=row.enabled,
        preset=row.preset,
        preset_label=PRESET_LABELS.get(row.preset, row.preset),
        cron=PRESETS.get(row.preset, "0 */4 * * *"),
        next_run_at=_next_run_time(),
        last_run_at=row.last_run_at,
        last_run_status=row.last_run_status,
        last_run_found=row.last_run_found,
        last_run_new=row.last_run_new,
        last_run_offline=row.last_run_offline,
        presets=PRESET_LABELS,
    )


@router.put("", response_model=ScheduleOut)
def update_schedule(
    payload: ScheduleUpdate,
    current_user: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    if payload.preset not in PRESETS:
        raise HTTPException(status_code=400, detail=f"Invalid preset. Choose from: {list(PRESETS)}")

    row = _get_or_create_schedule(db)
    row.enabled = payload.enabled
    row.preset  = payload.preset
    db.commit()

    # Apply to live scheduler without restart
    try:
        from scanner.scheduler import _scheduler, run_scheduled_scan
        from apscheduler.triggers.cron import CronTrigger

        if _scheduler and _scheduler.running:
            cron = PRESETS[payload.preset]
            if payload.enabled:
                _scheduler.add_job(
                    run_scheduled_scan,
                    trigger=CronTrigger.from_crontab(cron),
                    id="network_scan",
                    replace_existing=True,
                )
                log.info(f"Schedule updated live: {payload.preset} ({cron})")
            else:
                try:
                    _scheduler.remove_job("network_scan")
                    log.info("Scheduled scan disabled")
                except Exception:
                    pass
    except Exception as e:
        log.warning(f"Could not update live scheduler: {e}")

    auth.log_action(db, current_user.id, "update_schedule", "schedule", 1,
                    f"preset={payload.preset} enabled={payload.enabled}")

    db.refresh(row)
    return ScheduleOut(
        enabled=row.enabled,
        preset=row.preset,
        preset_label=PRESET_LABELS.get(row.preset, row.preset),
        cron=PRESETS.get(row.preset, "0 */4 * * *"),
        next_run_at=_next_run_time(),
        last_run_at=row.last_run_at,
        last_run_status=row.last_run_status,
        last_run_found=row.last_run_found,
        last_run_new=row.last_run_new,
        last_run_offline=row.last_run_offline,
        presets=PRESET_LABELS,
    )


# ── Alert config ──────────────────────────────────────────────────────────────

class AlertConfigOut(BaseModel):
    enabled:             bool
    smtp_host:           Optional[str] = None
    smtp_port:           int = 587
    smtp_username:       Optional[str] = None
    smtp_password_set:   bool = False    # never return the actual password
    smtp_use_tls:        bool = True
    from_address:        Optional[str] = None
    recipients:          Optional[str] = None
    alert_on_complete:   bool = False
    alert_on_new_device: bool = True
    alert_on_offline:    bool = True

    class Config:
        from_attributes = True


class AlertConfigUpdate(BaseModel):
    enabled:             bool
    smtp_host:           Optional[str] = None
    smtp_port:           int = 587
    smtp_username:       Optional[str] = None
    smtp_password:       Optional[str] = None   # None = keep existing
    smtp_use_tls:        bool = True
    from_address:        Optional[str] = None
    recipients:          Optional[str] = None
    alert_on_complete:   bool = False
    alert_on_new_device: bool = True
    alert_on_offline:    bool = True


@router.get("/alerts", response_model=AlertConfigOut)
def get_alerts(
    _: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    row = _get_or_create_alert(db)
    return AlertConfigOut(
        enabled=row.enabled,
        smtp_host=row.smtp_host,
        smtp_port=row.smtp_port or 587,
        smtp_username=row.smtp_username,
        smtp_password_set=bool(row.smtp_password),
        smtp_use_tls=row.smtp_use_tls if row.smtp_use_tls is not None else True,
        from_address=row.from_address,
        recipients=row.recipients,
        alert_on_complete=row.alert_on_complete or False,
        alert_on_new_device=row.alert_on_new_device if row.alert_on_new_device is not None else True,
        alert_on_offline=row.alert_on_offline if row.alert_on_offline is not None else True,
    )


@router.put("/alerts", response_model=AlertConfigOut)
def update_alerts(
    payload: AlertConfigUpdate,
    current_user: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    row = _get_or_create_alert(db)
    row.enabled             = payload.enabled
    row.smtp_host           = payload.smtp_host
    row.smtp_port           = payload.smtp_port
    row.smtp_username       = payload.smtp_username
    row.smtp_use_tls        = payload.smtp_use_tls
    row.from_address        = payload.from_address
    row.recipients          = payload.recipients
    row.alert_on_complete   = payload.alert_on_complete
    row.alert_on_new_device = payload.alert_on_new_device
    row.alert_on_offline    = payload.alert_on_offline

    # Only update password if a new one was provided
    if payload.smtp_password:
        row.smtp_password = encrypt_password(payload.smtp_password)

    db.commit()
    db.refresh(row)
    auth.log_action(db, current_user.id, "update_alerts", "alert_config", 1,
                    f"enabled={payload.enabled}")

    return AlertConfigOut(
        enabled=row.enabled,
        smtp_host=row.smtp_host,
        smtp_port=row.smtp_port or 587,
        smtp_username=row.smtp_username,
        smtp_password_set=bool(row.smtp_password),
        smtp_use_tls=row.smtp_use_tls,
        from_address=row.from_address,
        recipients=row.recipients,
        alert_on_complete=row.alert_on_complete or False,
        alert_on_new_device=row.alert_on_new_device,
        alert_on_offline=row.alert_on_offline,
    )


@router.post("/alerts/test", status_code=204)
def test_alert(
    current_user: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    """Send a test email to verify SMTP settings."""
    from scanner.alerts import _send_email, _html_wrap
    row = _get_or_create_alert(db)
    if not row.smtp_host:
        raise HTTPException(status_code=400, detail="SMTP not configured")
    ok = _send_email(
        row,
        subject="NetTrack — test alert",
        body_html=_html_wrap("Test alert", "<p style='font-size:13px;'>Your NetTrack email alerts are working correctly.</p>"),
        body_text="Your NetTrack email alerts are working correctly.",
    )
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to send test email — check SMTP settings")
