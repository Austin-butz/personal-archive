"""Stable content hashing (DESIGN.md §3)."""
import hashlib
import json
import unicodedata
from datetime import datetime, timezone


def normalize(v):
    if isinstance(v, str):
        v = v.replace("\r\n", "\n").replace("\r", "\n")
        return unicodedata.normalize("NFC", v)
    if isinstance(v, dict):
        return {k: normalize(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [normalize(x) for x in v]
    return v  # int, float, bool, None


def content_hash(fields: dict) -> str:
    payload = json.dumps(normalize(fields), sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def utc_iso(dt: datetime) -> str:
    """UTC ISO-8601 with a Z suffix, second precision. Naive datetimes are taken as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_stamp() -> str:
    """Bookkeeping timestamp. Microseconds so runs order correctly as strings."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
