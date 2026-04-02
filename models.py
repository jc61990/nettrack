from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean, ForeignKey
from sqlalchemy.sql import func
from database import Base


class Device(Base):
    __tablename__ = "devices"

    id         = Column(Integer, primary_key=True, index=True)
    hostname   = Column(String(255), index=True)
    ip         = Column(String(45), index=True)
    mac        = Column(String(17), unique=True, index=True)
    type       = Column(String(64))
    status     = Column(String(32), default="Unknown")
    floor      = Column(String(16))
    location   = Column(String(255))
    switch     = Column(String(64), index=True)
    port       = Column(String(32))
    vlan       = Column(Integer)
    notes      = Column(Text, default="")
    last_seen  = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Switch(Base):
    __tablename__ = "switches"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String(64), unique=True, index=True)
    ip         = Column(String(45))
    location   = Column(String(255))
    floor      = Column(String(16))
    model      = Column(String(128))
    notes      = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True, index=True)
    email         = Column(String(255), unique=True, index=True, nullable=False)
    full_name     = Column(String(255))
    role          = Column(String(32), default="read_only")   # read_only | modify | admin
    auth_provider = Column(String(32), default="local")       # local | oidc
    password_hash = Column(String(255), nullable=True)        # null for SSO-only users
    refresh_token = Column(Text, nullable=True)
    is_active     = Column(Boolean, default=True)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())
    last_login    = Column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action      = Column(String(64), index=True)    # create, update, delete, login, etc.
    resource    = Column(String(64), index=True)    # device, switch, user, session
    resource_id = Column(Integer, nullable=True)
    detail      = Column(Text, nullable=True)       # human-readable change summary
    created_at  = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class ScanSchedule(Base):
    """
    Singleton row (id=1) storing the active scan schedule.
    Updated live via PUT /api/scan/schedule — no restart needed.
    """
    __tablename__ = "scan_schedule"

    id              = Column(Integer, primary_key=True, default=1)
    enabled         = Column(Boolean, default=False)
    preset          = Column(String(32), default="every_4h")   # hourly|every_2h|every_4h|daily
    last_run_at     = Column(DateTime(timezone=True), nullable=True)
    last_run_status = Column(String(32), nullable=True)        # ok | error
    last_run_found  = Column(Integer, nullable=True)           # devices found
    last_run_new    = Column(Integer, nullable=True)           # new devices
    last_run_offline= Column(Integer, nullable=True)           # devices that went offline
    updated_at      = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AlertConfig(Base):
    """
    Singleton row (id=1) storing email alert settings.
    """
    __tablename__ = "alert_config"

    id                  = Column(Integer, primary_key=True, default=1)
    enabled             = Column(Boolean, default=False)
    smtp_host           = Column(String(255), nullable=True)
    smtp_port           = Column(Integer, default=587)
    smtp_username       = Column(String(255), nullable=True)
    smtp_password       = Column(String(255), nullable=True)  # stored encrypted via Fernet
    smtp_use_tls        = Column(Boolean, default=True)
    from_address        = Column(String(255), nullable=True)
    recipients          = Column(Text, nullable=True)          # comma-separated
    alert_on_complete   = Column(Boolean, default=False)
    alert_on_new_device = Column(Boolean, default=True)
    alert_on_offline    = Column(Boolean, default=True)
    updated_at          = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Floor(Base):
    __tablename__ = "floors"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String(64), nullable=False)
    building   = Column(String(128), nullable=True)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Vlan(Base):
    __tablename__ = "vlans"

    id          = Column(Integer, primary_key=True, index=True)
    vlan_id     = Column(Integer, nullable=False, unique=True, index=True)
    name        = Column(String(64), nullable=True)
    description = Column(Text, nullable=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())


class DiscoveryQueue(Base):
    __tablename__ = "discovery_queue"

    id            = Column(Integer, primary_key=True, index=True)
    # Discovered data
    hostname      = Column(String(255), nullable=True)
    ip            = Column(String(45),  nullable=True)
    mac           = Column(String(17),  nullable=True, index=True)
    type          = Column(String(64),  nullable=True)
    status        = Column(String(32),  nullable=True)
    floor         = Column(String(16),  nullable=True)
    location      = Column(String(255), nullable=True)
    switch        = Column(String(64),  nullable=True)
    port          = Column(String(32),  nullable=True)
    vlan          = Column(Integer,     nullable=True)
    notes         = Column(Text,        nullable=True)
    # Queue metadata
    queue_state   = Column(String(16),  nullable=False, index=True)  # new|changed|known
    queue_status  = Column(String(16),  nullable=False, default="pending", index=True)  # pending|accepted|ignored|blocked
    diff          = Column(Text,        nullable=True)   # JSON diff for changed devices
    scan_id       = Column(String(64),  nullable=True)   # ties items to a specific scan run
    existing_id   = Column(Integer,     nullable=True)   # device.id if already in inventory
    discovered_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    reviewed_at   = Column(DateTime(timezone=True), nullable=True)
    reviewed_by   = Column(Integer,     ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class ScanConfig(Base):
    """
    Persistent scan configuration stored in the database.
    Singleton row (id=1). Survives deployments unlike config.yaml.
    """
    __tablename__ = "scan_config"

    id              = Column(Integer, primary_key=True, default=1)
    subnets         = Column(Text,    nullable=True)   # JSON array of {cidr, description}
    snmp_community  = Column(String(128), nullable=True, default="public")
    snmp_port       = Column(Integer, nullable=True, default=161)
    snmp_timeout    = Column(Integer, nullable=True, default=2)
    snmp_retries    = Column(Integer, nullable=True, default=1)
    ping_timeout_ms = Column(Integer, nullable=True, default=800)
    ping_workers    = Column(Integer, nullable=True, default=64)
    updated_at      = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
