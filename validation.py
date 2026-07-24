"""Deterministic spec validation, run between Claude generation and the
drawing engine. `validate_spec(mode, spec)` returns a list of human-readable
problem strings (empty list == valid). These messages are both fed back to
Claude for one self-correction attempt and, if that fails, surfaced to the
user verbatim - so they must name the actual conflict in plain language.

Tolerances are deliberately generous: the goal is to catch specs that would
draw wrong or throw, not to reject anything a competent draftsperson would
accept. False positives here cause needless retries/errors.
"""
import math

_TOL = 1.0            # mm slack for boundary comparisons
DEFAULT_WALL_T = 60


def validate_spec(mode: str, spec: dict) -> list[str]:
    if not isinstance(spec, dict):
        return ["The generated spec was not a valid object."]
    if mode == "floorplan":
        return _validate_floorplan(spec)
    if mode == "container":
        return _validate_container(spec)
    return []


def _dist(a, b) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


# ---------------------------------------------------------------------------
# Floor plan
# ---------------------------------------------------------------------------

def _validate_floorplan(spec: dict) -> list[str]:
    problems: list[str] = []
    walls = spec.get("walls") or []
    if not walls:
        problems.append("The floor plan has no walls, so there is nothing to draw.")

    wall_len: dict = {}
    for w in walls:
        wid = w.get("id")
        start, end = w.get("start"), w.get("end")
        if not (isinstance(start, (list, tuple)) and isinstance(end, (list, tuple))):
            problems.append(f"Wall {wid!r} is missing start/end coordinates.")
            continue
        length = _dist(start, end)
        if length <= _TOL:
            problems.append(f"Wall {wid!r} has zero length (its start and end are the same point).")
        if (w.get("thickness") or 0) <= 0:
            problems.append(f"Wall {wid!r} has a non-positive thickness.")
        wall_len[wid] = length

    per_wall: dict = {}
    for op in spec.get("openings") or []:
        wid = op.get("wall_id")
        typ = op.get("type", "opening")
        if wid not in wall_len:
            problems.append(f"A {typ} is placed on wall {wid!r}, which doesn't exist.")
            continue
        length = wall_len[wid]
        width = op.get("width") or 0
        pos = op.get("position_along_wall") or 0
        if width <= 0:
            problems.append(f"A {typ} on wall {wid!r} has a non-positive width.")
            continue
        lo, hi = pos - width / 2.0, pos + width / 2.0
        if lo < -_TOL or hi > length + _TOL:
            problems.append(
                f"A {width:.0f}mm {typ} centred at {pos:.0f}mm doesn't fit on wall "
                f"{wid!r} (which is {length:.0f}mm long)."
            )
        per_wall.setdefault(wid, []).append((lo, hi, typ))

    for wid, intervals in per_wall.items():
        intervals.sort()
        for i in range(1, len(intervals)):
            if intervals[i][0] < intervals[i - 1][1] - _TOL:
                problems.append(f"Two openings overlap on wall {wid!r}.")
                break

    problems += _validate_additional(spec.get("additional_elements"))
    return problems


# ---------------------------------------------------------------------------
# Container home
# ---------------------------------------------------------------------------

def _validate_container(spec: dict) -> list[str]:
    problems: list[str] = []
    c = spec.get("container") or {}
    length = c.get("length_mm") or 0
    for name in ("length_mm", "width_mm", "height_mm"):
        if (c.get(name) or 0) <= 0:
            problems.append(f"Container {name.replace('_mm','')} must be a positive number.")
    if length <= 0:
        return problems  # nothing else is meaningful without a length

    t = c.get("wall_thickness_mm") or DEFAULT_WALL_T
    interior = length - 2 * t

    levels = spec.get("levels") or []
    if levels:
        for i, lvl in enumerate(levels):
            label = lvl.get("title", f"level {i + 1}")
            problems += _validate_plan(lvl.get("plan") or {}, length, interior, label)
    else:
        problems += _validate_plan(spec.get("plan") or {}, length, interior, "")

    be = spec.get("back_elevation") or {}
    vent = be.get("vent_window")
    if vent:
        w = vent.get("width_mm") or 0
        p = vent.get("position_from_left_mm") or 0
        if w <= 0:
            problems.append("The vent window has a non-positive width.")
        elif p < -_TOL or p + w > length + _TOL:
            problems.append(f"The vent window at {p:.0f}mm doesn't fit within the {length:.0f}mm wall.")

    problems += _validate_additional(spec.get("additional_elements"))
    return problems


def _validate_plan(plan: dict, length: float, interior: float, where: str) -> list[str]:
    problems: list[str] = []
    ctx = f" on {where}" if where else ""

    kr = plan.get("kitchen_run")
    if kr:
        segs = kr.get("segments") or []
        for seg in segs:
            if (seg.get("width_mm") or 0) <= 0:
                problems.append(f"Kitchen segment {seg.get('label', '?')!r}{ctx} has a non-positive width.")
        total = sum(seg.get("width_mm") or 0 for seg in segs)
        if total > interior + _TOL:
            problems.append(
                f"The kitchen run{ctx} ({total:.0f}mm of fixtures) is longer than the interior "
                f"wall ({interior:.0f}mm) - use a longer container or fewer/narrower fixtures."
            )

    rooms = plan.get("rooms") or []
    if rooms:
        for r in rooms:
            if (r.get("width_mm") or 0) <= 0:
                problems.append(f"Room {r.get('name', r.get('type', '?'))!r}{ctx} has a non-positive width.")
        total = sum(r.get("width_mm") or 0 for r in rooms)
        # The engine scales rooms down to fit, so only flag gross overflow.
        if total > interior * 1.15:
            problems.append(
                f"The rooms{ctx} total {total:.0f}mm, well over the interior length "
                f"({interior:.0f}mm) - reduce room widths or use a longer container."
            )

    doors = []
    if plan.get("sliding_door"):
        doors.append(plan["sliding_door"])
    doors += plan.get("sliding_doors") or []
    positions = []
    for d in doors:
        w = d.get("width_mm") or 0
        p = d.get("position_from_left_mm") or 0
        if w <= 0:
            problems.append(f"A sliding door{ctx} has a non-positive width.")
            continue
        if p < -_TOL or p + w > length + _TOL:
            problems.append(f"A {w:.0f}mm sliding door at {p:.0f}mm{ctx} doesn't fit within the {length:.0f}mm front wall.")
        positions.append((p, p + w))
    positions.sort()
    for i in range(1, len(positions)):
        if positions[i][0] < positions[i - 1][1] - _TOL:
            problems.append(f"Two sliding doors overlap{ctx}.")
            break

    for win in plan.get("windows") or []:
        w = win.get("width_mm") or 0
        p = win.get("position_from_left_mm") or 0
        if w <= 0:
            problems.append(f"A window{ctx} has a non-positive width.")
        elif p < -_TOL or p + w > length + _TOL:
            problems.append(f"A {w:.0f}mm window at {p:.0f}mm{ctx} doesn't fit within the {length:.0f}mm wall.")

    return problems


def _validate_additional(elements) -> list[str]:
    problems: list[str] = []
    for el in elements or []:
        size = el.get("approx_size_mm") or []
        if len(size) < 2 or (size[0] or 0) <= 0 or (size[1] or 0) <= 0:
            problems.append(
                f"Additional element {el.get('label', '?')!r} has a missing or non-positive size."
            )
    return problems
