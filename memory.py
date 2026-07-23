"""Lightweight cross-session preferences + design history, fed to Claude as optional context."""
from storage import kv_get_json, kv_set_json

MEMORY_KEY = "memory_v1"
_HISTORY_CAP = 20
_CONTEXT_HISTORY_ITEMS = 5


def _default_memory() -> dict:
    return {"preferences": [], "history": {"floorplan": [], "container": []}}


def load_memory() -> dict:
    data = kv_get_json(MEMORY_KEY, default=None)
    if data is None:
        return _default_memory()
    data.setdefault("preferences", [])
    data.setdefault("history", {})
    data["history"].setdefault("floorplan", [])
    data["history"].setdefault("container", [])
    return data


def save_memory(mem: dict) -> None:
    kv_set_json(MEMORY_KEY, mem)


def add_preference(text: str) -> dict:
    mem = load_memory()
    mem["preferences"].append(text)
    save_memory(mem)
    return mem


def remove_preference(index: int) -> dict:
    mem = load_memory()
    if 0 <= index < len(mem["preferences"]):
        mem["preferences"].pop(index)
        save_memory(mem)
    return mem


def clear_memory() -> dict:
    mem = _default_memory()
    save_memory(mem)
    return mem


def log_design(mode: str, summary: str, mem: dict | None = None) -> None:
    mem = mem if mem is not None else load_memory()
    mem["history"].setdefault(mode, [])
    mem["history"][mode].append(summary)
    mem["history"][mode] = mem["history"][mode][-_HISTORY_CAP:]
    save_memory(mem)


def build_context_block(mode: str, mem: dict | None = None) -> str:
    mem = mem if mem is not None else load_memory()
    prefs = mem.get("preferences", [])
    history = mem.get("history", {}).get(mode, [])
    if not prefs and not history:
        return ""
    lines = [
        "Memory context (apply only where relevant; never override an explicit "
        "instruction in the current request):"
    ]
    if prefs:
        lines.append("Stored preferences:")
        lines.extend(f"- {p}" for p in prefs)
    if history:
        lines.append(f"Recent {mode} design history:")
        lines.extend(f"- {h}" for h in history[-_CONTEXT_HISTORY_ITEMS:])
    return "\n".join(lines)


def summarize_spec(mode: str, spec: dict) -> str:
    if mode == "floorplan":
        walls = spec.get("walls", [])
        rooms = spec.get("rooms", [])
        openings = spec.get("openings", [])
        room_names = ", ".join(r.get("name", "room") for r in rooms) or "no rooms"
        doors = sum(1 for o in openings if o.get("type") == "door")
        windows = sum(1 for o in openings if o.get("type") == "window")
        return f"{len(walls)} walls; rooms: {room_names}; {doors} door(s), {windows} window(s)"

    container = spec.get("container", {})
    parts = [
        f"{container.get('length_mm', '?')}x{container.get('width_mm', '?')}x"
        f"{container.get('height_mm', '?')}mm container"
    ]
    plan = spec.get("plan", {})
    kitchen_run = plan.get("kitchen_run")
    if kitchen_run:
        labels = ", ".join(s.get("label", "") for s in kitchen_run.get("segments", []))
        parts.append(f"kitchen run: {labels}")
    sliding_door = plan.get("sliding_door")
    if sliding_door:
        parts.append(f"sliding door {sliding_door.get('width_mm')}mm")
    return "; ".join(parts)
