from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
import models, auth
from database import get_db

router = APIRouter(prefix="/api/floors", tags=["Floors"])


class FloorBase(BaseModel):
    name:       str
    building:   Optional[str] = None
    sort_order: int = 0

class FloorCreate(FloorBase):
    pass

class FloorOut(FloorBase):
    id:         int
    class Config:
        from_attributes = True


@router.get("", response_model=List[FloorOut])
def list_floors(
    _: models.User = Depends(auth.require_role("read_only")),
    db: Session = Depends(get_db),
):
    return db.query(models.Floor).order_by(models.Floor.sort_order, models.Floor.name).all()


@router.post("", response_model=FloorOut, status_code=201)
def create_floor(
    payload: FloorCreate,
    current_user: models.User = Depends(auth.require_role("modify")),
    db: Session = Depends(get_db),
):
    floor = models.Floor(**payload.model_dump())
    db.add(floor)
    db.commit()
    db.refresh(floor)
    auth.log_action(db, current_user.id, "create", "floor", floor.id, floor.name)
    return floor


@router.put("/{floor_id}", response_model=FloorOut)
def update_floor(
    floor_id: int,
    payload: FloorCreate,
    current_user: models.User = Depends(auth.require_role("modify")),
    db: Session = Depends(get_db),
):
    floor = db.query(models.Floor).filter(models.Floor.id == floor_id).first()
    if not floor:
        raise HTTPException(status_code=404, detail="Floor not found")
    for field, value in payload.model_dump().items():
        setattr(floor, field, value)
    db.commit()
    db.refresh(floor)
    auth.log_action(db, current_user.id, "update", "floor", floor_id, floor.name)
    return floor


@router.delete("/{floor_id}", status_code=204)
def delete_floor(
    floor_id: int,
    current_user: models.User = Depends(auth.require_role("modify")),
    db: Session = Depends(get_db),
):
    floor = db.query(models.Floor).filter(models.Floor.id == floor_id).first()
    if not floor:
        raise HTTPException(status_code=404, detail="Floor not found")
    auth.log_action(db, current_user.id, "delete", "floor", floor_id, floor.name)
    db.delete(floor)
    db.commit()
