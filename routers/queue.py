"""
Discovery queue endpoints.

POST /api/queue/ingest        — scanner posts discovered devices here
GET  /api/queue               — list queue items (filterable)
GET  /api/queue/counts        — pending counts by state
POST /api/queue/{id}/accept   — accept a device into inventory
POST /api/queue/{id}/ignore   — dismiss without adding
POST /api/queue/{id}/block    — block MAC from appearing again
PUT  /api/queue/{id}          — edit device data before accepting
POST /api/queue/bulk-accept   — accept all matching a filter
POST /api/queue/bulk-ignore   — ignore all matching a filter
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

import auth
import models
from database import get_db

router = APIRouter(prefix="/api/queue", tags=["Discovery queue"])

DEVICE_FIELDS = ['hostname','ip','mac','type','status','floor','location','switch','port','vlan','notes']


# ── Schemas ───────────────────────────────────────────────────────────────────

class QueueItemOut(BaseModel):
    id:            int
    hostname:      Optional[str] = None
    ip:            Optional[str] = None
    mac:           Optional[str] = None
    type:          Optional[str] = None
    status:        Optional[str] = None
    floor:         Optional[str] = None
    location:      Optional[str] = None
    switch:        Optional[str] = None
    port:          Optional[str] = None
    vlan:          Optional[int] = None
    notes:         Optional[str] = None
    queue_state:   str
    queue_status:  str
    diff:          Optional[str] = None
    scan_id:       Optional[str] = None
    existing_id:   Optional[int] = None
    discovered_at: Optional[datetime] = None
    reviewed_at:   Optional[datetime] = None
    class Config:
        from_attributes = True


class QueueCounts(BaseModel):
    pending_new:     int
    pending_changed: int
    pending_known:   int
    total_pending:   int


class IngestDevice(BaseModel):
    hostname: Optional[str] = None
    ip:       Optional[str] = None
    mac:      Optional[str] = None
    type:     Optional[str] = None
    status:   Optional[str] = None
    floor:    Optional[str] = None
    location: Optional[str] = None
    switch:   Optional[str] = None
    port:     Optional[str] = None
    vlan:     Optional[int] = None
    notes:    Optional[str] = None


class IngestRequest(BaseModel):
    devices: List[IngestDevice]
    scan_id: Optional[str] = None


class DeviceEdit(BaseModel):
    hostname: Optional[str] = None
    ip:       Optional[str] = None
    mac:      Optional[str] = None
    type:     Optional[str] = None
    status:   Optional[str] = None
    floor:    Optional[str] = None
    location: Optional[str] = None
    switch:   Optional[str] = None
    port:     Optional[str] = None
    vlan:     Optional[int] = None
    notes:    Optional[str] = None


class BulkActionRequest(BaseModel):
    queue_state: Optional[str] = None   # new | changed | known
    type:        Optional[str] = None
    switch:      Optional[str] = None
    floor:       Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _compute_diff(existing: models.Device, incoming: dict) -> dict:
    """Return dict of {field: {old, new}} for fields that changed."""
    diff = {}
    for f in DEVICE_FIELDS:
        old_val = getattr(existing, f, None)
        new_val = incoming.get(f)
        if old_val != new_val and not (old_val is None and not new_val):
            diff[f] = {'old': old_val, 'new': new_val}
    return diff


def _apply_to_inventory(item: models.DiscoveryQueue, db: Session,
                         user_id: int) -> models.Device:
    """Write a queue item into the devices table."""
    try:
        if item.existing_id:
            device = db.query(models.Device).filter(models.Device.id == item.existing_id).first()
            if device:
                for f in DEVICE_FIELDS:
                    val = getattr(item, f)
                    if val is not None:
                        setattr(device, f, val)
                db.commit()
                db.refresh(device)
                return device

        # New device — only set non-None fields
        fields = {f: getattr(item, f) for f in DEVICE_FIELDS if getattr(item, f) is not None}
        device = models.Device(**fields)
        db.add(device)
        db.commit()
        db.refresh(device)
        return device
    except Exception as e:
        db.rollback()
        raise


def _filter_query(q, payload: BulkActionRequest):
    if payload.queue_state: q = q.filter(models.DiscoveryQueue.queue_state == payload.queue_state)
    if payload.type:        q = q.filter(models.DiscoveryQueue.type == payload.type)
    if payload.switch:      q = q.filter(models.DiscoveryQueue.switch == payload.switch)
    if payload.floor:       q = q.filter(models.DiscoveryQueue.floor == payload.floor)
    return q


# ── Ingest ────────────────────────────────────────────────────────────────────

@router.post("/ingest", status_code=201)
def ingest(
    payload: IngestRequest,
    current_user: models.User = Depends(auth.require_role("modify")),
    db: Session = Depends(get_db),
):
    """
    Called by the scanner after a sweep. Classifies each device as:
      new     — MAC not in inventory
      changed — MAC in inventory but fields differ
      known   — MAC in inventory, nothing changed
    Skips blocked MACs entirely.
    """
    scan_id  = payload.scan_id or str(uuid.uuid4())[:8]
    counts   = {'new': 0, 'changed': 0, 'known': 0, 'blocked': 0}

    # Load blocked MACs
    blocked = {
        row.mac for row in
        db.query(models.DiscoveryQueue.mac).filter(
            models.DiscoveryQueue.queue_status == 'blocked'
        ).distinct().all()
        if row.mac
    }

    for item in payload.devices:
        # Allow devices without MAC — use IP as dedup key
        dedup_key = item.mac or item.ip
        if not dedup_key:
            continue
        if item.mac and item.mac in blocked:
            counts['blocked'] += 1
            continue

        incoming = item.model_dump()
        # Look up existing device by MAC if available, otherwise by IP
        existing = None
        if item.mac:
            existing = db.query(models.Device).filter(models.Device.mac == item.mac).first()
        if not existing and item.ip:
            existing = db.query(models.Device).filter(models.Device.ip == item.ip).first()

        if not existing:
            state = 'new'
            diff  = None
            existing_id = None
        else:
            diff_data = _compute_diff(existing, incoming)
            if diff_data:
                state = 'changed'
                diff  = json.dumps(diff_data)
            else:
                state = 'known'
                diff  = None
            existing_id = existing.id

        # Remove old pending entry for same device (replace with fresh scan data)
        q = db.query(models.DiscoveryQueue).filter(
            models.DiscoveryQueue.queue_status == 'pending',
        )
        if item.mac:
            q = q.filter(models.DiscoveryQueue.mac == item.mac)
        elif item.ip:
            q = q.filter(models.DiscoveryQueue.ip == item.ip)
        q.delete()

        entry = models.DiscoveryQueue(
            **incoming,
            queue_state=state,
            queue_status='pending',
            diff=diff,
            scan_id=scan_id,
            existing_id=existing_id,
        )
        db.add(entry)
        counts[state] += 1

    db.commit()
    auth.log_action(db, current_user.id, "scan_ingest", "queue", None,
                    f"scan={scan_id} new={counts['new']} changed={counts['changed']} "
                    f"known={counts['known']} blocked={counts['blocked']}")
    return {**counts, 'scan_id': scan_id}


# ── List & counts ─────────────────────────────────────────────────────────────

@router.get("/counts", response_model=QueueCounts)
def get_counts(
    _: models.User = Depends(auth.require_role("read_only")),
    db: Session = Depends(get_db),
):
    base = db.query(models.DiscoveryQueue).filter(
        models.DiscoveryQueue.queue_status == 'pending'
    )
    new     = base.filter(models.DiscoveryQueue.queue_state == 'new').count()
    changed = base.filter(models.DiscoveryQueue.queue_state == 'changed').count()
    known   = base.filter(models.DiscoveryQueue.queue_state == 'known').count()
    return QueueCounts(
        pending_new=new,
        pending_changed=changed,
        pending_known=known,
        total_pending=new + changed + known,
    )


@router.get("", response_model=List[QueueItemOut])
def list_queue(
    queue_status: Optional[str] = Query("pending"),
    queue_state:  Optional[str] = Query(None),
    type:         Optional[str] = Query(None),
    switch:       Optional[str] = Query(None),
    floor:        Optional[str] = Query(None),
    _: models.User = Depends(auth.require_role("read_only")),
    db: Session = Depends(get_db),
):
    q = db.query(models.DiscoveryQueue)
    if queue_status: q = q.filter(models.DiscoveryQueue.queue_status == queue_status)
    if queue_state:  q = q.filter(models.DiscoveryQueue.queue_state  == queue_state)
    if type:         q = q.filter(models.DiscoveryQueue.type   == type)
    if switch:       q = q.filter(models.DiscoveryQueue.switch  == switch)
    if floor:        q = q.filter(models.DiscoveryQueue.floor   == floor)
    return q.order_by(
        models.DiscoveryQueue.queue_state,   # new first, then changed, then known
        models.DiscoveryQueue.discovered_at.desc()
    ).all()


# ── Single item actions ───────────────────────────────────────────────────────

@router.put("/{item_id}", response_model=QueueItemOut)
def edit_queue_item(
    item_id: int,
    payload: DeviceEdit,
    _: models.User = Depends(auth.require_role("modify")),
    db: Session = Depends(get_db),
):
    item = db.query(models.DiscoveryQueue).filter(models.DiscoveryQueue.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Queue item not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
    db.commit()
    db.refresh(item)
    return item


@router.post("/{item_id}/accept", response_model=QueueItemOut)
def accept_item(
    item_id: int,
    current_user: models.User = Depends(auth.require_role("modify")),
    db: Session = Depends(get_db),
):
    item = db.query(models.DiscoveryQueue).filter(models.DiscoveryQueue.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Queue item not found")

    device = _apply_to_inventory(item, db, current_user.id)
    item.queue_status = 'accepted'
    item.reviewed_at  = datetime.now(timezone.utc)
    item.reviewed_by  = current_user.id
    db.commit()
    db.refresh(item)
    auth.log_action(db, current_user.id, "accept", "queue", item_id,
                    f"{item.hostname or item.ip} → device #{device.id}")
    return item


@router.post("/{item_id}/ignore", response_model=QueueItemOut)
def ignore_item(
    item_id: int,
    current_user: models.User = Depends(auth.require_role("modify")),
    db: Session = Depends(get_db),
):
    item = db.query(models.DiscoveryQueue).filter(models.DiscoveryQueue.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Queue item not found")
    item.queue_status = 'ignored'
    item.reviewed_at  = datetime.now(timezone.utc)
    item.reviewed_by  = current_user.id
    db.commit()
    db.refresh(item)
    auth.log_action(db, current_user.id, "ignore", "queue", item_id,
                    item.hostname or item.ip or item.mac)
    return item


@router.post("/{item_id}/block", response_model=QueueItemOut)
def block_item(
    item_id: int,
    current_user: models.User = Depends(auth.require_role("modify")),
    db: Session = Depends(get_db),
):
    """Block this MAC — it will never appear in the queue again."""
    item = db.query(models.DiscoveryQueue).filter(models.DiscoveryQueue.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Queue item not found")
    item.queue_status = 'blocked'
    item.reviewed_at  = datetime.now(timezone.utc)
    item.reviewed_by  = current_user.id
    db.commit()
    db.refresh(item)
    auth.log_action(db, current_user.id, "block", "queue", item_id,
                    f"MAC {item.mac} blocked")
    return item


# ── Bulk actions ──────────────────────────────────────────────────────────────

@router.post("/bulk-accept")
def bulk_accept(
    payload: BulkActionRequest,
    current_user: models.User = Depends(auth.require_role("modify")),
    db: Session = Depends(get_db),
):
    q = db.query(models.DiscoveryQueue).filter(
        models.DiscoveryQueue.queue_status == 'pending'
    )
    q = _filter_query(q, payload)
    items   = q.all()
    count   = 0
    now     = datetime.now(timezone.utc)
    for item in items:
        _apply_to_inventory(item, db, current_user.id)
        item.queue_status = 'accepted'
        item.reviewed_at  = now
        item.reviewed_by  = current_user.id
        count += 1
    db.commit()
    auth.log_action(db, current_user.id, "bulk_accept", "queue", None,
                    f"{count} devices accepted")
    return {'accepted': count}


@router.post("/bulk-ignore")
def bulk_ignore(
    payload: BulkActionRequest,
    current_user: models.User = Depends(auth.require_role("modify")),
    db: Session = Depends(get_db),
):
    q = db.query(models.DiscoveryQueue).filter(
        models.DiscoveryQueue.queue_status == 'pending'
    )
    q = _filter_query(q, payload)
    items = q.all()
    count = 0
    now   = datetime.now(timezone.utc)
    for item in items:
        item.queue_status = 'ignored'
        item.reviewed_at  = now
        item.reviewed_by  = current_user.id
        count += 1
    db.commit()
    auth.log_action(db, current_user.id, "bulk_ignore", "queue", None,
                    f"{count} devices ignored")
    return {'ignored': count}
