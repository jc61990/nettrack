"""
Scanner configuration.

Sensitive values (SNMP credentials, API token) are read from environment
variables — never stored in config.yaml. The YAML file holds topology and
tuning only.

Environment variables:
    SNMP_COMMUNITY          SNMPv2c community string (default: public)
    SNMP_V3_USERNAME        SNMPv3 username
    SNMP_V3_AUTH_KEY        SNMPv3 auth passphrase
    SNMP_V3_PRIV_KEY        SNMPv3 priv passphrase
    SNMP_V3_AUTH_PROTOCOL   sha | sha256 | md5  (default: sha)
    SNMP_V3_PRIV_PROTOCOL   aes | aes192 | des  (default: aes)
    SCANNER_API_TOKEN       Bearer token for bulk-upsert endpoint
    SCANNER_CONFIG          Path to config.yaml (default: scanner/config.yaml)
"""

import os
from dataclasses import dataclass, field
from typing import Optional
import yaml

CONFIG_PATH = os.environ.get(
    "SCANNER_CONFIG",
    os.path.join(os.path.dirname(__file__), "config.yaml"),
)


@dataclass
class SNMPv3Creds:
    username:      str
    auth_key:      str
    priv_key:      str
    auth_protocol: str = "sha"
    priv_protocol: str = "aes"


@dataclass
class SubnetConfig:
    cidr:           str
    description:    str = ""
    # Per-subnet community override — if None, falls back to global env var
    snmp_community: Optional[str] = None


@dataclass
class ScannerConfig:
    subnets:         list[SubnetConfig] = field(default_factory=list)
    snmp_community:  str = "public"
    snmp_v3:         Optional[SNMPv3Creds] = None
    ping_timeout_ms: int = 800
    ping_workers:    int = 64
    snmp_port:       int = 161
    snmp_timeout:    int = 2
    snmp_retries:    int = 1
    results_dir:     str = "/var/log/nettrack/scans"
    api_url:         str = "http://localhost:8000"
    api_token:       str = ""
    schedule_enabled: bool = False
    schedule_cron:   str = "0 */4 * * *"


def _snmp_v3_from_env() -> Optional[SNMPv3Creds]:
    """Build SNMPv3 creds from environment variables. Returns None if not configured."""
    username = os.environ.get("SNMP_V3_USERNAME")
    auth_key = os.environ.get("SNMP_V3_AUTH_KEY")
    priv_key = os.environ.get("SNMP_V3_PRIV_KEY")
    if not (username and auth_key and priv_key):
        return None
    return SNMPv3Creds(
        username=username,
        auth_key=auth_key,
        priv_key=priv_key,
        auth_protocol=os.environ.get("SNMP_V3_AUTH_PROTOCOL", "sha"),
        priv_protocol=os.environ.get("SNMP_V3_PRIV_PROTOCOL", "aes"),
    )


def load_config(path: str = CONFIG_PATH) -> ScannerConfig:
    raw: dict = {}
    if os.path.exists(path):
        with open(path) as f:
            raw = yaml.safe_load(f) or {}

    # Credentials always come from environment — never from YAML
    snmp_community = os.environ.get("SNMP_COMMUNITY", raw.get("snmp_community", "public"))
    snmp_v3        = _snmp_v3_from_env()
    api_token      = os.environ.get("SCANNER_API_TOKEN", "")

    subnets = [
        SubnetConfig(
            cidr=s["cidr"],
            description=s.get("description", ""),
            # Subnets may specify a different community name (not the key itself)
            snmp_community=s.get("snmp_community"),
        )
        for s in raw.get("subnets", [])
    ]

    return ScannerConfig(
        subnets=subnets,
        snmp_community=snmp_community,
        snmp_v3=snmp_v3,
        ping_timeout_ms=raw.get("ping_timeout_ms", 800),
        ping_workers=raw.get("ping_workers", 64),
        snmp_port=raw.get("snmp_port", 161),
        snmp_timeout=raw.get("snmp_timeout", 2),
        snmp_retries=raw.get("snmp_retries", 1),
        results_dir=raw.get("results_dir", "/var/log/nettrack/scans"),
        api_url=raw.get("api_url", "http://localhost:8000"),
        api_token=api_token,
        schedule_enabled=raw.get("schedule_enabled", False),
        schedule_cron=raw.get("schedule_cron", "0 */4 * * *"),
    )
