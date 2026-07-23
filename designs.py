"""Saved-design dashboard storage: an index (metadata only) plus full records, keyed by id."""
import uuid
from datetime import datetime, timezone

from storage import kv_delete, kv_get_json, kv_set_json

INDEX_KEY = "designs_index_v1"


def _record_key(design_id: str) -> str:
    return f"design_record_{design_id}"


def list_designs() -> list:
    return kv_get_json(INDEX_KEY, default=[])


def save_design(mode: str, title: str, spec: dict, design_id: str | None = None) -> dict:
    timestamp = datetime.now(timezone.utc).isoformat()

    if design_id:
        existing = kv_get_json(_record_key(design_id))
        created_at = existing["created_at"] if existing else timestamp
    else:
        design_id = str(uuid.uuid4())
        created_at = timestamp

    record = {
        "id": design_id,
        "title": title,
        "mode": mode,
        "spec": spec,
        "created_at": created_at,
        "updated_at": timestamp,
    }
    kv_set_json(_record_key(design_id), record)

    meta = {"id": design_id, "title": title, "mode": mode, "updated_at": timestamp}
    index = [d for d in list_designs() if d["id"] != design_id]
    index.insert(0, meta)
    kv_set_json(INDEX_KEY, index)
    return meta


def get_design(design_id: str) -> dict | None:
    return kv_get_json(_record_key(design_id))


def delete_design(design_id: str) -> bool:
    index = list_designs()
    new_index = [d for d in index if d["id"] != design_id]
    existed = len(new_index) != len(index) or get_design(design_id) is not None
    if not existed:
        return False
    kv_set_json(INDEX_KEY, new_index)
    kv_delete(_record_key(design_id))
    return True
