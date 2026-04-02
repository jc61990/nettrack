from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import List, Optional
import models, schemas, auth
from database import get_db

router = APIRouter(prefix="/api/audit", tags=["Audit log"])


@router.get("", response_model=List[schemas.AuditLogOut])
def get_audit_log(
    resource: Optional[str] = Query(None),
    user_id: Optional[int] = Query(None),
    action: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0),
    _: models.User = Depends(auth.require_role("admin")),
    db: Session = Depends(get_db),
):
    q = db.query(models.AuditLog)
    if resource:
        q = q.filter(models.AuditLog.resource == resource)
    if user_id:
        q = q.filter(models.AuditLog.user_id == user_id)
    if action:
        q = q.filter(models.AuditLog.action == action)
    return (
        q.order_by(models.AuditLog.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
