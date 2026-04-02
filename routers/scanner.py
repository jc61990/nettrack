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
from scanner.config import load_config
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


@router.post('/trigger')
def trigger_scan(
    request: ScanRequest,
    current_user: models.User = Depends(auth.require_role('modify')),
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
