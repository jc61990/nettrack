"""
Scanner API router.

POST /api/scan/trigger   — start a scan (streams live output via SSE)
GET  /api/scan/status    — last scan summary
GET  /api/scan/results   — list saved result files
"""

import asyncio
import json
import threading
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import auth
import models
from sqlalchemy.orm import Session
from database import get_db
from scanner.config import load_config
import yaml
import os
from scanner.scanner import NetworkScanner, save_results, post_results, check_ping_capability

router = APIRouter(prefix='/api/scan', tags=['Scanner'])

# In-memory scan state (one scan at a time)
_scan_lock   = threading.Lock()
_scan_running = False
_last_scan: dict = {}


class ScanRequest(BaseModel):
    subnets:        Optional[list[str]] = None   # overrides config if provided
    snmp_community: Optional[str]       = None
    dry_run:        bool                = False   # scan but don't POST to API


class ScanStatus(BaseModel):
    running:      bool
    last_scan:    Optional[dict] = None
    ping_method:  Optional[str] = None
    ping_warning: Optional[str] = None


@router.get('/config')
def get_scan_config(
    _: models.User = Depends(auth.require_role('read_only')),
    db: Session = Depends(get_db),
):
    """Return scan config from database."""
    row = db.query(models.ScanConfig).filter(models.ScanConfig.id == 1).first()
    if not row:
        row = models.ScanConfig(id=1)
        db.add(row)
        db.commit()
        db.refresh(row)
    subnets = json.loads(row.subnets) if row.subnets else []
    return {
        'subnets':         subnets,
        'snmp_community':  row.snmp_community or 'public',
        'snmp_port':       row.snmp_port       or 161,
        'snmp_timeout':    row.snmp_timeout     or 2,
        'snmp_retries':    row.snmp_retries     or 1,
        'ping_timeout_ms': row.ping_timeout_ms  or 800,
        'ping_workers':    row.ping_workers      or 64,
    }


@router.put('/config', status_code=200)
def save_scan_config(
    payload: dict,
    current_user: models.User = Depends(auth.require_role('modify')),
    db: Session = Depends(get_db),
):
    """Save scan config to database — persists through deployments."""
    row = db.query(models.ScanConfig).filter(models.ScanConfig.id == 1).first()
    if not row:
        row = models.ScanConfig(id=1)
        db.add(row)

    if 'subnets' in payload:
        row.subnets = json.dumps(payload['subnets'])
    if 'snmp_community'  in payload: row.snmp_community  = payload['snmp_community']
    if 'snmp_port'       in payload: row.snmp_port        = int(payload['snmp_port'])
    if 'snmp_timeout'    in payload: row.snmp_timeout     = int(payload['snmp_timeout'])
    if 'snmp_retries'    in payload: row.snmp_retries     = int(payload['snmp_retries'])
    if 'ping_timeout_ms' in payload: row.ping_timeout_ms  = int(payload['ping_timeout_ms'])
    if 'ping_workers'    in payload: row.ping_workers      = int(payload['ping_workers'])

    db.commit()
    auth.log_action(db, current_user.id, "update_scan_config", "scanner", None,
                    f"{len(payload.get('subnets', []))} subnets")
    return {'saved': True}


@router.post('/trigger')
def trigger_scan(
    request: ScanRequest,
    current_user: models.User = Depends(auth.require_role('modify')),
    db: Session = Depends(get_db),
):
    """
    Start a network scan and stream live log output as Server-Sent Events.

    The response is a text/event-stream where each event is:
        data: {"level": "ok"|"warn"|"info", "msg": "..."}

    Final event:
        data: {"level": "done", "created": N, "updated": N, "saved": "/path/to/file"}
    """
    global _scan_running, _last_scan

    if _scan_running:
        raise HTTPException(status_code=409, detail='A scan is already in progress')

    config = load_config()

    # Load DB-stored scan config (subnets + tuning saved by user in UI)
    try:
        db_cfg = db.query(models.ScanConfig).filter(models.ScanConfig.id == 1).first()
        if db_cfg:
            if db_cfg.subnets:
                db_subnets = json.loads(db_cfg.subnets)
                if db_subnets:
                    from scanner.config import SubnetConfig as SC
                    config.subnets = [SC(cidr=s['cidr'], description=s.get('description',''))
                                      for s in db_subnets if s.get('cidr')]
            if db_cfg.snmp_community:  config.snmp_community  = db_cfg.snmp_community
            if db_cfg.snmp_port:       config.snmp_port        = db_cfg.snmp_port
            if db_cfg.snmp_timeout:    config.snmp_timeout     = db_cfg.snmp_timeout
            if db_cfg.snmp_retries:    config.snmp_retries     = db_cfg.snmp_retries
            if db_cfg.ping_timeout_ms: config.ping_timeout_ms  = db_cfg.ping_timeout_ms
            if db_cfg.ping_workers:    config.ping_workers      = db_cfg.ping_workers
    except Exception as e:
        log.warning(f"Could not load DB scan config: {e}")

    # Request-level overrides (manual scan page can override subnets/community)
    if request.subnets:
        from scanner.config import SubnetConfig as SC
        config.subnets = [SC(cidr=s) for s in request.subnets]
    if request.snmp_community:
        config.snmp_community = request.snmp_community

    def event_generator():
        global _scan_running, _last_scan

        with _scan_lock:
            _scan_running = True

        log_lines = []

        def emit(level: str, msg: str):
            line = json.dumps({'level': level, 'msg': msg})
            log_lines.append(line)

        try:
            scanner = NetworkScanner(config)
            results = scanner.scan(subnets=request.subnets, emit=emit)

            filepath = None
            if results:
                filepath = save_results(results, config.results_dir)
                emit('ok', f'[✓] Results saved: {filepath}')

                if not request.dry_run:
                    ok = post_results(results, config.api_url, config.api_token)
                    if ok:
                        emit('ok', '[✓] Inventory updated in NetTrack')
                    else:
                        emit('warn', '[!] Failed to POST results to API — check api_url/api_token in config')

            _last_scan = {
                'scanned_at':    datetime.utcnow().isoformat(),
                'device_count':  len(results),
                'saved_file':    filepath,
                'triggered_by':  current_user.email,
            }

            # Yield all buffered lines then the done event
            for line in log_lines:
                yield f'data: {line}\n\n'

            yield f'data: {json.dumps({"level": "done", "device_count": len(results), "saved": filepath})}\n\n'

        except Exception as e:
            yield f'data: {json.dumps({"level": "warn", "msg": f"[!] Scan error: {e}"})}\n\n'
            yield f'data: {json.dumps({"level": "done", "device_count": 0, "saved": None})}\n\n'
        finally:
            _scan_running = False

    return StreamingResponse(
        event_generator(),
        media_type='text/event-stream',
        headers={
            'Cache-Control':   'no-cache',
            'X-Accel-Buffering': 'no',   # disable nginx buffering for SSE
        },
    )


@router.get('/status', response_model=ScanStatus)
def scan_status(_: models.User = Depends(auth.require_role('read_only'))):
    capability = check_ping_capability()
    return ScanStatus(
        running=_scan_running,
        last_scan=_last_scan or None,
        ping_method=capability.get('method'),
        ping_warning=capability.get('warning'),
    )


@router.get('/results')
def list_results(_: models.User = Depends(auth.require_role('read_only'))):
    """List saved scan result files."""
    config = load_config()
    import os
    if not os.path.isdir(config.results_dir):
        return []
    files = sorted(
        [f for f in os.listdir(config.results_dir) if f.endswith('.json')],
        reverse=True,
    )
    return [{'filename': f, 'path': os.path.join(config.results_dir, f)} for f in files[:50]]
