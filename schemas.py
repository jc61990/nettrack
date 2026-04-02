from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List
from datetime import datetime


# ── Device schemas ────────────────────────────────────────────────────────────

class DeviceBase(BaseModel):
    hostname:  Optional[str] = None
    ip:        Optional[str] = None
    mac:       Optional[str] = None
    type:      Optional[str] = None
    status:    Optional[str] = "Unknown"
    floor:     Optional[str] = None
    location:  Optional[str] = None
    switch:    Optional[str] = None
    port:      Optional[str] = None
    vlan:      Optional[int] = None
    notes:     Optional[str] = ""

class DeviceCreate(DeviceBase):
    pass

class DeviceUpdate(DeviceBase):
    pass

class DeviceOut(DeviceBase):
    id:         int
    last_seen:  Optional[datetime] = None
    created_at: Optional[datetime] = None
    class Config:
        from_attributes = True


# ── Bulk upsert schemas ───────────────────────────────────────────────────────

class BulkUpsertRequest(BaseModel):
    devices: List[DeviceCreate]

class BulkUpsertResult(BaseModel):
    created: int
    updated: int


# ── Switch schemas ────────────────────────────────────────────────────────────

class SwitchBase(BaseModel):
    name:     str
    ip:       Optional[str] = None
    location: Optional[str] = None
    floor:    Optional[str] = None
    model:    Optional[str] = None
    notes:    Optional[str] = ""

class SwitchCreate(SwitchBase):
    pass

class SwitchOut(SwitchBase):
    id:         int
    created_at: Optional[datetime] = None
    class Config:
        from_attributes = True


# ── User schemas ──────────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    email:     str
    full_name: str
    role:      str = Field(default="read_only", pattern="^(read_only|modify|admin)$")
    password:  Optional[str] = None   # optional — omit for SSO-only accounts

class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    role:      Optional[str] = Field(default=None, pattern="^(read_only|modify|admin)$")
    is_active: Optional[bool] = None
    password:  Optional[str] = None   # set to reset password

class UserOut(BaseModel):
    id:            int
    email:         str
    full_name:     Optional[str] = None
    role:          str
    auth_provider: str
    is_active:     bool
    created_at:    Optional[datetime] = None
    last_login:    Optional[datetime] = None
    class Config:
        from_attributes = True


# ── Auth schemas ──────────────────────────────────────────────────────────────

class TokenResponse(BaseModel):
    access_token:  str
    refresh_token: str
    token_type:    str = "bearer"

class RefreshRequest(BaseModel):
    refresh_token: str


# ── Audit log schemas ─────────────────────────────────────────────────────────

class AuditLogOut(BaseModel):
    id:          int
    user_id:     Optional[int] = None
    action:      str
    resource:    str
    resource_id: Optional[int] = None
    detail:      Optional[str] = None
    created_at:  datetime
    class Config:
        from_attributes = True
