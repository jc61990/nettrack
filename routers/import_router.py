"""
Excel import endpoints.

POST /api/import/preview   — upload xlsx, get back header mapping suggestions + sample rows
POST /api/import/confirm   — send confirmed mapping + all rows → ingest to discovery queue
                             auto-creates missing floors and VLANs
"""

import io
import json
import re
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.orm import Session

import auth
import models
from database import get_db

router = APIRouter(prefix="/api/import", tags=["Import"])

# ── Field definitions ─────────────────────────────────────────────────────────

DEVICE_FIELDS = [
    "hostname", "ip", "mac", "type", "status",
    "floor", "location", "switch", "port", "vlan", "notes",
]

FIELD_LABELS = {
    "hostname": "Hostname",
    "ip":       "IP Address",
    "mac":      "MAC Address",
    "type":     "Device Type",
    "status":   "Status",
    "floor":    "Floor",
    "location": "Location",
    "switch":   "Switch",
    "port":     "Port",
    "vlan":     "VLAN",
    "notes":    "Notes",
}

# Fuzzy match aliases — lowercase header → field name
ALIASES: Dict[str, str] = {
    # hostname
    "hostname": "hostname", "host": "hostname", "host name": "hostname",
    "device name": "hostname", "name": "hostname", "device": "hostname",
    "fqdn": "hostname", "dns name": "hostname",
    # ip
    "ip": "ip", "ip address": "ip", "ipaddress": "ip", "ip addr": "ip",
    "address": "ip", "ipv4": "ip", "management ip": "ip", "mgmt ip": "ip",
    # mac
    "mac": "mac", "mac address": "mac", "macaddress": "mac",
    "hardware address": "mac", "ethernet": "mac", "physical address": "mac",
    # type
    "type": "type", "device type": "type", "category": "type",
    "equipment type": "type", "kind": "type",
    # status
    "status": "status", "state": "status", "condition": "status",
    "online": "status", "active": "status",
    # floor
    "floor": "floor", "level": "floor", "floor number": "floor",
    "story": "floor", "storey": "floor", "building level": "floor",
    # location
    "location": "location", "room": "location", "area": "location",
    "zone": "location", "position": "location", "description": "location",
    "physical location": "location", "site": "location",
    # switch
    "switch": "switch", "switch name": "switch", "connected switch": "switch",
    "uplink switch": "switch", "parent switch": "switch", "sw": "switch",
    # port
    "port": "port", "switch port": "port", "interface": "port",
    "port number": "port", "connected port": "port", "gi": "port",
    # vlan
    "vlan": "vlan", "vlan id": "vlan", "vlan number": "vlan",
    "network": "vlan", "vlan#": "vlan",
    # notes
    "notes": "notes", "note": "notes", "comments": "notes",
    "comment": "notes", "remarks": "notes", "info": "notes",
}

VALID_TYPES = {"Camera", "Access Control", "Intercom", "Switch", "WAP",
               "Server", "Printer", "Other"}

TYPE_ALIASES = {
    "cam": "Camera", "camera": "Camera", "cctv": "Camera", "ip camera": "Camera",
    "nvr": "Camera", "dvr": "Camera",
    "access control": "Access Control", "ac": "Access Control", "door": "Access Control",
    "reader": "Access Control", "access": "Access Control",
    "intercom": "Intercom", "video intercom": "Intercom", "door station": "Intercom",
    "switch": "Switch", "sw": "Switch", "network switch": "Switch", "managed switch": "Switch",
    "wap": "WAP", "ap": "WAP", "access point": "WAP", "wifi": "WAP",
    "wireless": "WAP", "wireless ap": "WAP",
    "server": "Server", "nas": "Server", "vm": "Server",
    "printer": "Printer", "print": "Printer", "mfp": "Printer",
}


def _auto_map(headers: List[str]) -> Dict[str, Optional[str]]:
    """Return {excel_col: device_field|None} for each header."""
    mapping = {}
    used = set()
    for h in headers:
        key = re.sub(r'[_\-\.]+', ' ', h.strip().lower())
        field = ALIASES.get(key)
        if field and field not in used:
            mapping[h] = field
            used.add(field)
        else:
            mapping[h] = None
    return mapping


def _normalize_mac(val: str) -> Optional[str]:
    """Normalize MAC address to XX:XX:XX:XX:XX format from any common notation."""
    if not val:
        return None
    # Strip all separators and spaces
    raw = re.sub(r'[.:\s-]', '', val.strip().upper())
    if len(raw) != 12 or not re.match(r'^[0-9A-F]+$', raw):
        return val  # Return as-is if can't parse — let DB truncation catch it
    return ':'.join(raw[i:i+2] for i in range(0, 12, 2))


def _normalize_type(val: str) -> str:
    if not val:
        return "Other"
    v = val.strip().lower()
    return TYPE_ALIASES.get(v, val if val in VALID_TYPES else "Other")


def _normalize_status(val: str) -> str:
    if not val:
        return "Unknown"
    v = val.strip().lower()
    if v in {"online", "up", "active", "yes", "1", "true"}:
        return "Online"
    if v in {"offline", "down", "inactive", "no", "0", "false"}:
        return "Offline"
    return "Unknown"


# ── Schemas ───────────────────────────────────────────────────────────────────

class PreviewResponse(BaseModel):
    headers:     List[str]
    mapping:     Dict[str, Optional[str]]
    sample_rows: List[Dict[str, Any]]
    total_rows:  int
    all_rows:    List[Dict[str, Any]]   # full data for confirm step


class ConfirmRequest(BaseModel):
    mapping:  Dict[str, Optional[str]]   # excel_col → device_field
    rows:     List[Dict[str, Any]]        # raw rows from preview
    # Fields the user filled in for rows with missing required data
    # {row_index: {field: value}}
    overrides: Optional[Dict[str, Dict[str, Any]]] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/preview", response_model=PreviewResponse)
async def preview_import(
    file: UploadFile = File(...),
    _: models.User = Depends(auth.require_role("modify")),
):
    """Upload an Excel file and get back header mapping suggestions."""
    if not file.filename.endswith(('.xlsx', '.xls', '.csv')):
        raise HTTPException(status_code=400,
                            detail="Only .xlsx, .xls, and .csv files are supported")
    try:
        import openpyxl
        content = await file.read()

        if file.filename.endswith('.csv'):
            import csv as _csv
            text    = content.decode('utf-8-sig', errors='replace')
            reader  = _csv.DictReader(io.StringIO(text))
            headers = reader.fieldnames or []
            rows    = [dict(r) for r in reader]
        else:
            wb   = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
            ws   = wb.active
            data = list(ws.iter_rows(values_only=True))
            if not data:
                raise HTTPException(status_code=400, detail="Spreadsheet is empty")
            headers = [str(h).strip() if h is not None else f"Column_{i}"
                       for i, h in enumerate(data[0])]
            rows    = [
                {headers[i]: (str(cell).strip() if cell is not None else "")
                 for i, cell in enumerate(row)}
                for row in data[1:]
                if any(cell is not None and str(cell).strip() for cell in row)
            ]

        mapping     = _auto_map(headers)
        sample_rows = rows[:5]

        return PreviewResponse(
            headers=headers,
            mapping=mapping,
            sample_rows=sample_rows,
            total_rows=len(rows),
            all_rows=rows,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read file: {e}")


@router.post("/confirm")
async def confirm_import(
    payload: ConfirmRequest,
    current_user: models.User = Depends(auth.require_role("modify")),
    db: Session = Depends(get_db),
):
    """
    Apply confirmed column mapping, auto-create missing floors/VLANs,
    and send all devices to the discovery queue.
    """
    import uuid as _uuid

    mapping   = payload.mapping
    overrides = payload.overrides or {}
    scan_id   = f"import_{str(_uuid.uuid4())[:6]}"

    # Load existing floors, VLANs, and switches
    existing_floors   = {f.name.lower(): f for f in db.query(models.Floor).all()}
    existing_vlans    = {v.vlan_id: v for v in db.query(models.Vlan).all()}
    existing_switches = {s.name.lower(): s for s in db.query(models.Switch).all()}

    created_floors:   List[str] = []
    created_vlans:    List[int] = []
    created_switches: List[str] = []
    skipped:          List[int] = []
    queued:           int = 0

    # Load blocked MACs
    blocked = {
        row.mac for row in
        db.query(models.DiscoveryQueue.mac).filter(
            models.DiscoveryQueue.queue_status == 'blocked'
        ).distinct().all()
        if row.mac
    }

    for idx, raw_row in enumerate(payload.rows):
        row_overrides = overrides.get(str(idx), {})

        # Apply mapping
        device: Dict[str, Any] = {}
        for excel_col, field in mapping.items():
            if not field:
                continue
            val = str(raw_row.get(excel_col, "") or "").strip()
            if val:
                device[field] = val

        # Apply overrides
        device.update(row_overrides)

        # Skip if no IP and no hostname and no MAC — nothing to identify it
        if not device.get('ip') and not device.get('hostname') and not device.get('mac'):
            skipped.append(idx)
            continue

        # Normalize type and status
        if 'type' in device:
            device['type'] = _normalize_type(device['type'])
        if 'status' in device:
            device['status'] = _normalize_status(device['status'])
        else:
            device['status'] = 'Unknown'

        # Normalize VLAN to int
        if 'vlan' in device:
            try:
                device['vlan'] = int(float(device['vlan']))
            except (ValueError, TypeError):
                del device['vlan']

        # Auto-create floor if it doesn't exist
        if 'floor' in device and device['floor']:
            floor_key = device['floor'].lower()
            if floor_key not in existing_floors:
                new_floor = models.Floor(name=device['floor'])
                db.add(new_floor)
                db.flush()
                existing_floors[floor_key] = new_floor
                created_floors.append(device['floor'])

        # Auto-create VLAN if it doesn't exist
        if 'vlan' in device and device['vlan']:
            vlan_id = device['vlan']
            if vlan_id not in existing_vlans:
                new_vlan = models.Vlan(vlan_id=vlan_id)
                db.add(new_vlan)
                db.flush()
                existing_vlans[vlan_id] = new_vlan
                created_vlans.append(vlan_id)

        # Auto-create switch if it doesn't exist
        if 'switch' in device and device['switch']:
            sw_key = device['switch'].lower()
            if sw_key not in existing_switches:
                # Try to carry over floor if we have it
                sw_floor = device.get('floor')
                new_sw = models.Switch(
                    name=device['switch'],
                    floor=sw_floor,
                )
                db.add(new_sw)
                db.flush()
                existing_switches[sw_key] = new_sw
                created_switches.append(device['switch'])

        # Normalize MAC address format
        if device.get('mac'):
            device['mac'] = _normalize_mac(device['mac'])

        # Skip blocked MACs
        mac = device.get('mac') or None
        if mac and mac in blocked:
            skipped.append(idx)
            continue

        # Clean empty strings to None
        device = {k: (v if v != '' else None) for k, v in device.items()}

        # Look up existing device
        existing_device = None
        if mac:
            existing_device = db.query(models.Device).filter(models.Device.mac == mac).first()
        if not existing_device and device.get('ip'):
            existing_device = db.query(models.Device).filter(
                models.Device.ip == device['ip']
            ).first()

        # Compute diff if existing
        diff      = None
        state     = 'new'
        existing_id = None
        if existing_device:
            existing_id = existing_device.id
            diff_data = {}
            for f in ['hostname','ip','mac','type','status','floor','location',
                      'switch','port','vlan','notes']:
                old_val = getattr(existing_device, f, None)
                new_val = device.get(f)
                if old_val != new_val and not (old_val is None and not new_val):
                    diff_data[f] = {'old': old_val, 'new': new_val}
            if diff_data:
                state = 'changed'
                diff  = json.dumps(diff_data)
            else:
                state = 'known'

        # Remove old pending entry
        q = db.query(models.DiscoveryQueue).filter(
            models.DiscoveryQueue.queue_status == 'pending'
        )
        if mac:
            q.filter(models.DiscoveryQueue.mac == mac).delete()
        elif device.get('ip'):
            q.filter(models.DiscoveryQueue.ip == device['ip']).delete()

        entry = models.DiscoveryQueue(
            hostname=device.get('hostname'),
            ip=device.get('ip'),
            mac=device.get('mac'),
            type=device.get('type', 'Other'),
            status=device.get('status', 'Unknown'),
            floor=device.get('floor'),
            location=device.get('location'),
            switch=device.get('switch'),
            port=device.get('port'),
            vlan=device.get('vlan'),
            notes=device.get('notes'),
            queue_state=state,
            queue_status='pending',
            diff=diff,
            scan_id=scan_id,
            existing_id=existing_id,
        )
        db.add(entry)
        queued += 1

    db.commit()

    auth.log_action(db, current_user.id, "import", "queue", None,
                    f"import={scan_id} queued={queued} skipped={len(skipped)} "
                    f"floors_created={len(created_floors)} vlans_created={len(created_vlans)} "
                    f"switches_created={len(created_switches)}")

    return {
        "queued":            queued,
        "skipped":           len(skipped),
        "skipped_rows":      skipped,
        "created_floors":    created_floors,
        "created_vlans":     created_vlans,
        "created_switches":  created_switches,
        "scan_id":           scan_id,
    }
