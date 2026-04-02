"""
NetTrack email alert engine.

Sends alerts for:
  - Scan complete (summary of what changed)
  - New device discovered
  - Device went offline

SMTP password is stored encrypted in the database using Fernet symmetric
encryption keyed from SECRET_KEY, so plain-text passwords never sit in
the DB unprotected.
"""

import base64
import logging
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

log = logging.getLogger(__name__)

# ── Encryption helpers ────────────────────────────────────────────────────────

def _fernet() -> Fernet:
    """Derive a Fernet key from SECRET_KEY using PBKDF2."""
    secret = os.environ.get("SECRET_KEY", "").encode()
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"nettrack-smtp-salt",
        iterations=100_000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(secret))
    return Fernet(key)

def encrypt_password(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()

def decrypt_password(encrypted: str) -> str:
    return _fernet().decrypt(encrypted.encode()).decode()


# ── SMTP sender ───────────────────────────────────────────────────────────────

def _send_email(cfg, subject: str, body_html: str, body_text: str) -> bool:
    """
    Send an email using the AlertConfig settings.
    Returns True on success, False on failure.
    """
    if not cfg.smtp_host or not cfg.from_address or not cfg.recipients:
        log.warning("Alert email not sent — SMTP config incomplete")
        return False

    recipients = [r.strip() for r in cfg.recipients.split(",") if r.strip()]
    if not recipients:
        return False

    try:
        password = decrypt_password(cfg.smtp_password) if cfg.smtp_password else ""

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = cfg.from_address
        msg["To"]      = ", ".join(recipients)
        msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        if cfg.smtp_use_tls:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15)
            server.ehlo()
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=15)

        if cfg.smtp_username and password:
            server.login(cfg.smtp_username, password)

        server.sendmail(cfg.from_address, recipients, msg.as_string())
        server.quit()
        log.info(f"Alert email sent to {recipients}: {subject}")
        return True

    except Exception as e:
        log.error(f"Failed to send alert email: {e}")
        return False


# ── Alert templates ───────────────────────────────────────────────────────────

def _html_wrap(title: str, body: str) -> str:
    return f"""
<!DOCTYPE html>
<html>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
             background:#f7f7f6;margin:0;padding:2rem;">
  <div style="max-width:560px;margin:0 auto;background:#fff;
              border-radius:8px;border:1px solid #e0e0dd;overflow:hidden;">
    <div style="background:#1a1a18;padding:1rem 1.5rem;">
      <span style="color:#fff;font-size:16px;font-weight:600;">NetTrack</span>
    </div>
    <div style="padding:1.5rem;">
      <h2 style="margin:0 0 1rem;font-size:18px;color:#1a1a18;">{title}</h2>
      {body}
    </div>
    <div style="padding:0.75rem 1.5rem;background:#f7f7f6;
                font-size:11px;color:#9b9b97;border-top:1px solid #e0e0dd;">
      Sent by NetTrack · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
    </div>
  </div>
</body>
</html>"""

def _device_table(devices: list[dict]) -> str:
    if not devices:
        return "<p style='color:#6b6b67;'>None</p>"
    rows = "".join(
        f"<tr style='border-bottom:1px solid #f0f0ee;'>"
        f"<td style='padding:6px 8px;font-family:monospace;font-size:13px;'>{d.get('hostname','—')}</td>"
        f"<td style='padding:6px 8px;font-family:monospace;font-size:13px;color:#6b6b67;'>{d.get('ip','—')}</td>"
        f"<td style='padding:6px 8px;font-size:13px;'>{d.get('type','—')}</td>"
        f"<td style='padding:6px 8px;font-size:13px;'>{d.get('switch','—')} {d.get('port','')}</td>"
        f"</tr>"
        for d in devices[:20]   # cap at 20 rows per email
    )
    overflow = f"<p style='font-size:12px;color:#9b9b97;'>…and {len(devices)-20} more</p>" \
               if len(devices) > 20 else ""
    return f"""
<table style="width:100%;border-collapse:collapse;font-size:13px;">
  <thead>
    <tr style="background:#f7f7f6;">
      <th style="padding:6px 8px;text-align:left;font-size:11px;color:#6b6b67;">Hostname</th>
      <th style="padding:6px 8px;text-align:left;font-size:11px;color:#6b6b67;">IP</th>
      <th style="padding:6px 8px;text-align:left;font-size:11px;color:#6b6b67;">Type</th>
      <th style="padding:6px 8px;text-align:left;font-size:11px;color:#6b6b67;">Switch / Port</th>
    </tr>
  </thead>
  <tbody>{rows}</tbody>
</table>{overflow}"""


# ── Public alert functions ─────────────────────────────────────────────────────

def send_scan_complete(cfg, stats: dict) -> bool:
    """Send a scan completion summary email."""
    if not cfg.enabled or not cfg.alert_on_complete:
        return False

    total    = stats.get("total", 0)
    new      = stats.get("new", 0)
    updated  = stats.get("updated", 0)
    offline  = stats.get("offline", 0)
    duration = stats.get("duration_seconds", 0)

    subject = f"NetTrack scan complete — {total} devices found"
    body_html = _html_wrap("Scan complete", f"""
        <div style="display:flex;gap:1rem;margin-bottom:1rem;flex-wrap:wrap;">
          <div style="background:#EAF3DE;border-radius:6px;padding:10px 16px;text-align:center;">
            <div style="font-size:24px;font-weight:600;color:#3B6D11;">{total}</div>
            <div style="font-size:11px;color:#3B6D11;">Total devices</div>
          </div>
          <div style="background:#E6F1FB;border-radius:6px;padding:10px 16px;text-align:center;">
            <div style="font-size:24px;font-weight:600;color:#185FA5;">{new}</div>
            <div style="font-size:11px;color:#185FA5;">New</div>
          </div>
          <div style="background:#f1f0ec;border-radius:6px;padding:10px 16px;text-align:center;">
            <div style="font-size:24px;font-weight:600;color:#5F5E5A;">{updated}</div>
            <div style="font-size:11px;color:#5F5E5A;">Updated</div>
          </div>
          <div style="background:#FCEBEB;border-radius:6px;padding:10px 16px;text-align:center;">
            <div style="font-size:24px;font-weight:600;color:#A32D2D;">{offline}</div>
            <div style="font-size:11px;color:#A32D2D;">Offline</div>
          </div>
        </div>
        <p style="font-size:13px;color:#6b6b67;">Scan completed in {duration}s.</p>""")
    body_text = (
        f"NetTrack scan complete\n\n"
        f"Total: {total}  New: {new}  Updated: {updated}  Offline: {offline}\n"
        f"Duration: {duration}s"
    )
    return _send_email(cfg, subject, body_html, body_text)


def send_new_devices(cfg, new_devices: list[dict]) -> bool:
    """Send an alert listing newly discovered devices."""
    if not cfg.enabled or not cfg.alert_on_new_device or not new_devices:
        return False

    count   = len(new_devices)
    subject = f"NetTrack — {count} new device{'s' if count != 1 else ''} discovered"
    body_html = _html_wrap(
        f"{count} new device{'s' if count != 1 else ''} found on the network",
        f"<p style='font-size:13px;color:#6b6b67;margin-bottom:1rem;'>"
        f"The following device{'s were' if count != 1 else ' was'} discovered "
        f"during the latest scan:</p>"
        + _device_table(new_devices)
    )
    body_text = (
        f"{count} new device(s) discovered:\n\n"
        + "\n".join(f"  {d.get('hostname','?')} ({d.get('ip','?')}) — {d.get('type','?')}"
                    for d in new_devices[:20])
    )
    return _send_email(cfg, subject, body_html, body_text)


def send_offline_devices(cfg, offline_devices: list[dict]) -> bool:
    """Send an alert listing devices that stopped responding."""
    if not cfg.enabled or not cfg.alert_on_offline or not offline_devices:
        return False

    count   = len(offline_devices)
    subject = f"NetTrack — {count} device{'s' if count != 1 else ''} offline"
    body_html = _html_wrap(
        f"{count} device{'s' if count != 1 else ''} no longer responding",
        f"<p style='font-size:13px;color:#A32D2D;margin-bottom:1rem;'>"
        f"The following device{'s' if count != 1 else ''} did not respond "
        f"during the latest scan:</p>"
        + _device_table(offline_devices)
    )
    body_text = (
        f"{count} device(s) offline:\n\n"
        + "\n".join(f"  {d.get('hostname','?')} ({d.get('ip','?')}) — {d.get('switch','?')} {d.get('port','?')}"
                    for d in offline_devices[:20])
    )
    return _send_email(cfg, subject, body_html, body_text)
