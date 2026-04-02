from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
import models, auth
from database import get_db

router = APIRouter(prefix="/api/vlans", tags=["VLANs"])


class VlanBase(BaseModel):
    vlan_id:     int
    name:        Optional[str] = None
    description: Optional[str] = None

class VlanCreate(VlanBase):
    pass

class VlanOut(VlanBase):
    id: int
    class Config:
        from_attributes = True


@router.get("", response_model=List[VlanOut])
def list_vlans(
    _: models.User = Depends(auth.require_role("read_only")),
    db: Session = Depends(get_db),
):
    return db.query(models.Vlan).order_by(models.Vlan.vlan_id).all()


@router.post("", response_model=VlanOut, status_code=201)
def create_vlan(
    payload: VlanCreate,
    current_user: models.User = Depends(auth.require_role("modify")),
    db: Session = Depends(get_db),
):
    if db.query(models.Vlan).filter(models.Vlan.vlan_id == payload.vlan_id).first():
        raise HTTPException(status_code=409, detail=f"VLAN {payload.vlan_id} already exists")
    vlan = models.Vlan(**payload.model_dump())
    db.add(vlan)
    db.commit()
    db.refresh(vlan)
    auth.log_action(db, current_user.id, "create", "vlan", vlan.id,
                    f"VLAN {vlan.vlan_id} {vlan.name or ''}")
    return vlan


@router.put("/{vlan_db_id}", response_model=VlanOut)
def update_vlan(
    vlan_db_id: int,
    payload: VlanCreate,
    current_user: models.User = Depends(auth.require_role("modify")),
    db: Session = Depends(get_db),
):
    vlan = db.query(models.Vlan).filter(models.Vlan.id == vlan_db_id).first()
    if not vlan:
        raise HTTPException(status_code=404, detail="VLAN not found")
    # Check duplicate vlan_id if changing it
    existing = db.query(models.Vlan).filter(
        models.Vlan.vlan_id == payload.vlan_id,
        models.Vlan.id != vlan_db_id,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"VLAN {payload.vlan_id} already exists")
    for field, value in payload.model_dump().items():
        setattr(vlan, field, value)
    db.commit()
    db.refresh(vlan)
    auth.log_action(db, current_user.id, "update", "vlan", vlan_db_id,
                    f"VLAN {vlan.vlan_id} {vlan.name or ''}")
    return vlan


@router.delete("/{vlan_db_id}", status_code=204)
def delete_vlan(
    vlan_db_id: int,
    current_user: models.User = Depends(auth.require_role("modify")),
    db: Session = Depends(get_db),
):
    vlan = db.query(models.Vlan).filter(models.Vlan.id == vlan_db_id).first()
    if not vlan:
        raise HTTPException(status_code=404, detail="VLAN not found")
    auth.log_action(db, current_user.id, "delete", "vlan", vlan_db_id,
                    f"VLAN {vlan.vlan_id}")
    db.delete(vlan)
    db.commit()
