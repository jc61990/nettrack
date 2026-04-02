"""
Scheduled scanner — loads config from DB on each run, fires email alerts.
Starts at boot if the DB has enabled=True in scan_schedule.
Schedule can be updated live via PUT /api/scan/schedule without restart.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from scanner.config import load_config
from scanner.scanner import NetworkScanner, save_results, post_results

log = logging.getLogger(__name__)
_scheduler: Optional[BackgroundScheduler] = None

PRESETS = {
    "hourly":   "0 * * * *",
    "every_2h": "0 */2 * * *",
    "every_4h": "0 */4 * * *",
    "daily":    "0 2 * * *",
}


def run_scheduled_scan():
    """
    Called by APScheduler. Runs a full scan, fires alerts, updates DB stats.
    Loads fresh config from DB on every run so credential/subnet changes
    take effect without restarting the service.
    """
    from database import SessionLocal
    import models
    from scanner.alerts import send_scan_complete, send_new_devices, send_offline_devices

    db = SessionLocal()
    start = time.time()

    try:
        config    = load_config()
        scanner   = NetworkScanner(config)
        log.info("Scheduled scan starting...")

        # Snapshot current device statuses before scan
        existing = {d.mac: d for d in db.query(models.Device).all() if d.mac}

        results = scanner.scan(emit=lambda level, msg: log.info(msg))

        if not results:
            log.warning("Scan returned no results")
            _update_schedule_row(db, status="error", found=0, new=0, offline=0)
            return

        # Save raw results to disk
        filepath = save_results(results, config.results_dir)
        log.info(f"Results saved: {filepath}")

        # Detect new and offline devices before posting
        scanned_macs = {r["mac"] for r in results if r.get("mac")}

        new_devices     = [r for r in results if r.get("mac") and r["mac"] not in existing]
        offline_devices = [
            {"hostname": d.hostname, "ip": d.ip, "mac": d.mac,
             "type": d.type, "switch": d.switch, "port": d.port}
            for mac, d in existing.items()
            if mac not in scanned_macs and d.status == "Online"
        ]

        # Post to API
        ok = post_results(results, config.api_url, config.api_token)
        if ok:
            log.info("Inventory updated in NetTrack")
        else:
            log.warning("Failed to POST results to API")

        duration = int(time.time() - start)
        stats = {
            "total":            len(results),
            "new":              len(new_devices),
            "updated":          len(results) - len(new_devices),
            "offline":          len(offline_devices),
            "duration_seconds": duration,
        }
        log.info(f"Scan complete: {stats}")

        # Update schedule row with stats
        _update_schedule_row(
            db,
            status="ok",
            found=stats["total"],
            new=stats["new"],
            offline=stats["offline"],
        )

        # Send alerts
        alert_cfg = db.query(models.AlertConfig).filter(models.AlertConfig.id == 1).first()
        if alert_cfg and alert_cfg.enabled:
            if new_devices:
                send_new_devices(alert_cfg, new_devices)
            if offline_devices:
                send_offline_devices(alert_cfg, offline_devices)
            send_scan_complete(alert_cfg, stats)

    except Exception as e:
        log.error(f"Scheduled scan failed: {e}", exc_info=True)
        _update_schedule_row(db, status="error", found=0, new=0, offline=0)
    finally:
        db.close()


def _update_schedule_row(db, status: str, found: int, new: int, offline: int):
    import models
    row = db.query(models.ScanSchedule).filter(models.ScanSchedule.id == 1).first()
    if row:
        row.last_run_at      = datetime.now(timezone.utc)
        row.last_run_status  = status
        row.last_run_found   = found
        row.last_run_new     = new
        row.last_run_offline = offline
        db.commit()


def start_scheduler():
    global _scheduler
    _scheduler = BackgroundScheduler()
    _scheduler.start()
    log.info("APScheduler started")

    # Load schedule from DB
    try:
        from database import SessionLocal
        import models
        db  = SessionLocal()
        row = db.query(models.ScanSchedule).filter(models.ScanSchedule.id == 1).first()
        db.close()

        if row and row.enabled:
            cron = PRESETS.get(row.preset, "0 */4 * * *")
            _scheduler.add_job(
                run_scheduled_scan,
                trigger=CronTrigger.from_crontab(cron),
                id="network_scan",
                replace_existing=True,
            )
            log.info(f"Scheduled scan enabled: {row.preset} ({cron})")
        else:
            log.info("Scheduled scan is disabled — enable via admin UI")
    except Exception as e:
        log.warning(f"Could not load schedule from DB at startup: {e}")


def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("APScheduler stopped")
