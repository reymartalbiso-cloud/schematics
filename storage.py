"""Pluggable key-value storage: Upstash Redis REST API when configured, local JSON file otherwise."""
import json
import os
from pathlib import Path

import requests

UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

_LOCAL_DIR = Path(__file__).resolve().parent / "memory_data"
_LOCAL_FILE = _LOCAL_DIR / "kv_store.json"


def _upstash_enabled() -> bool:
    return bool(UPSTASH_URL and UPSTASH_TOKEN)


def _upstash(method: str, path: str, data: bytes | None = None):
    resp = requests.request(
        method,
        f"{UPSTASH_URL}/{path}",
        headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
        data=data,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _local_load() -> dict:
    if not _LOCAL_FILE.exists():
        return {}
    try:
        return json.loads(_LOCAL_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _local_save(store: dict) -> None:
    _LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    _LOCAL_FILE.write_text(json.dumps(store), encoding="utf-8")


def kv_get(key: str) -> str | None:
    if _upstash_enabled():
        return _upstash("GET", f"get/{key}").get("result")
    return _local_load().get(key)


def kv_set(key: str, value: str) -> None:
    if _upstash_enabled():
        _upstash("POST", f"set/{key}", data=value.encode("utf-8"))
        return
    store = _local_load()
    store[key] = value
    _local_save(store)


def kv_delete(key: str) -> None:
    if _upstash_enabled():
        _upstash("POST", f"del/{key}")
        return
    store = _local_load()
    store.pop(key, None)
    _local_save(store)


def kv_get_json(key: str, default=None):
    raw = kv_get(key)
    return json.loads(raw) if raw else default


def kv_set_json(key: str, value) -> None:
    kv_set(key, json.dumps(value))
