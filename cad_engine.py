"""CAD-generation engine: turns a JSON floor-plan spec into an in-memory ezdxf
Drawing, and serializes that drawing to DXF bytes or a PNG preview image.

No disk writes anywhere in this module.
"""

import math

import ezdxf

from dxf_render import doc_to_dxf_bytes, doc_to_preview_bytes  # noqa: F401 - re-exported
from dxf_render import add as _add
from dxf_render import draw_measurement
from dxf_render import length as _length
from dxf_render import perp as _perp
from dxf_render import scale as _scale
from dxf_render import sub as _sub
from dxf_render import unit as _unit


# Monochrome, weight-differentiated linework (name -> lineweight in 1/100
# mm): heavy walls, medium openings, thin dimensions - matching the
# black-on-white shop-drawing references rather than colored CAD layers.
LAYER_DEFS = {
    "WALLS": 50,
    "DOORS": 25,
    "WINDOWS": 18,
    "TEXT": 25,
    "DIMS": 13,
}


def _wall_vectors(wall):
    start = tuple(wall["start"])
    end = tuple(wall["end"])
    direction = _unit(_sub(end, start))
    perpendicular = _perp(direction)
    return start, end, direction, perpendicular


def _wall_opening_intervals(wall, openings):
    """(lo, hi) distances along the wall for each opening on it, clamped to
    the wall length and merged so overlapping openings cut one gap."""
    start = tuple(wall["start"])
    end = tuple(wall["end"])
    length = _length(_sub(end, start))
    raw = []
    for op in openings:
        if op.get("wall_id") != wall.get("id"):
            continue
        pos = op["position_along_wall"]
        w = op["width"]
        lo = max(0.0, pos - w / 2.0)
        hi = min(length, pos + w / 2.0)
        if hi > lo:
            raw.append((lo, hi))
    raw.sort()
    merged = []
    for lo, hi in raw:
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def _draw_walls(msp, walls, openings):
    for wall in walls:
        start, end, direction, perpendicular = _wall_vectors(wall)
        half = wall["thickness"] / 2.0
        offset = _scale(perpendicular, half)
        length = _length(_sub(end, start))
        intervals = _wall_opening_intervals(wall, openings)

        if not intervals:
            # No openings: a single closed rectangle reads as a solid wall.
            p1, p2 = _add(start, offset), _add(end, offset)
            p3, p4 = _sub(end, offset), _sub(start, offset)
            msp.add_lwpolyline([p1, p2, p3, p4], close=True, dxfattribs={"layer": "WALLS"})
            continue

        # End caps.
        msp.add_line(_add(start, offset), _sub(start, offset), dxfattribs={"layer": "WALLS"})
        msp.add_line(_add(end, offset), _sub(end, offset), dxfattribs={"layer": "WALLS"})

        def at(d):
            return _add(start, _scale(direction, d))

        # Both long faces, broken across each opening, with a jamb line
        # spanning the wall thickness at every opening edge.
        cursor = 0.0
        for lo, hi in intervals:
            if lo > cursor:
                a, b = at(cursor), at(lo)
                msp.add_line(_add(a, offset), _add(b, offset), dxfattribs={"layer": "WALLS"})
                msp.add_line(_sub(a, offset), _sub(b, offset), dxfattribs={"layer": "WALLS"})
            for edge in (lo, hi):
                p = at(edge)
                msp.add_line(_add(p, offset), _sub(p, offset), dxfattribs={"layer": "WALLS"})
            cursor = hi
        if cursor < length:
            a, b = at(cursor), at(length)
            msp.add_line(_add(a, offset), _add(b, offset), dxfattribs={"layer": "WALLS"})
            msp.add_line(_sub(a, offset), _sub(b, offset), dxfattribs={"layer": "WALLS"})


def _wall_by_id(walls, wall_id):
    for wall in walls:
        if wall["id"] == wall_id:
            return wall
    raise KeyError(f"wall_id {wall_id!r} not found in walls")


def _draw_door(msp, wall, opening):
    start, end, direction, perpendicular = _wall_vectors(wall)
    position = opening["position_along_wall"]
    width = opening["width"]
    center = _add(start, _scale(direction, position))
    jamb_a = _sub(center, _scale(direction, width / 2.0))
    jamb_b = _add(center, _scale(direction, width / 2.0))

    # Hinge is the jamb closer to the wall's start point.
    dist_a = _length(_sub(jamb_a, start))
    dist_b = _length(_sub(jamb_b, start))
    if dist_a <= dist_b:
        hinge, closed_jamb = jamb_a, jamb_b
    else:
        hinge, closed_jamb = jamb_b, jamb_a

    swing_sign = -1.0 if opening.get("swing") == "right" else 1.0
    swing_perp = _scale(perpendicular, swing_sign)
    open_tip = _add(hinge, _scale(swing_perp, width))

    msp.add_line(hinge, open_tip, dxfattribs={"layer": "DOORS"})

    # The arc sweeps 90 degrees between the leaf's open position (hinge -> open_tip)
    # and its closed position (hinge -> closed_jamb). Derive both angles from the
    # actual vectors so the sweep direction is always correct regardless of swing.
    angle_open = math.degrees(math.atan2(open_tip[1] - hinge[1], open_tip[0] - hinge[0]))
    angle_closed = math.degrees(math.atan2(closed_jamb[1] - hinge[1], closed_jamb[0] - hinge[0]))

    diff = (angle_closed - angle_open) % 360
    if diff <= 180:
        start_angle, end_angle = angle_open, angle_closed
    else:
        start_angle, end_angle = angle_closed, angle_open

    msp.add_arc(
        center=hinge,
        radius=width,
        start_angle=start_angle,
        end_angle=end_angle,
        dxfattribs={"layer": "DOORS"},
    )


def _draw_window(msp, wall, opening):
    start, end, direction, perpendicular = _wall_vectors(wall)
    position = opening["position_along_wall"]
    width = opening["width"]
    thickness = wall["thickness"]
    center = _add(start, _scale(direction, position))
    jamb_a = _sub(center, _scale(direction, width / 2.0))
    jamb_b = _add(center, _scale(direction, width / 2.0))

    for offset_dist in (-thickness / 2.0, 0.0, thickness / 2.0):
        offset = _scale(perpendicular, offset_dist)
        msp.add_line(
            _add(jamb_a, offset),
            _add(jamb_b, offset),
            dxfattribs={"layer": "WINDOWS"},
        )


def _draw_openings(msp, walls, openings):
    for opening in openings:
        wall = _wall_by_id(walls, opening["wall_id"])
        if opening["type"] == "door":
            _draw_door(msp, wall, opening)
        elif opening["type"] == "window":
            _draw_window(msp, wall, opening)


def _draw_rooms(msp, rooms):
    for room in rooms:
        text = room["name"]
        if room.get("area_sqm") is not None:
            text += f"\n{room['area_sqm']} m²"
        mtext = msp.add_mtext(text, dxfattribs={"layer": "TEXT", "char_height": 200})
        mtext.set_location(insert=tuple(room["label_position"]))


# Real ezdxf DIMENSION entities are correct, standards-compliant DXF (verified
# down to the per-entity XDATA override and the base DIMSTYLE table record),
# but real-world testing found LibreCAD never displays their text regardless
# of style/height configuration - a rendering gap specific to how it handles
# DIMENSION entities, since plain MTEXT renders fine in the same file. Drawn
# by hand instead, from the same LINE/MTEXT primitives already proven to
# render identically everywhere. Sized to sit in the same neighborhood as
# room-label text (char_height 200).
def _walls_bbox_center(walls):
    xs, ys = [], []
    for wall in walls:
        for pt in (wall["start"], wall["end"]):
            xs.append(pt[0])
            ys.append(pt[1])
    if not xs:
        return None
    return ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)


def _draw_dimensions(msp, dimensions, walls):
    center = _walls_bbox_center(walls)
    for dim in dimensions:
        start = tuple(dim["start"])
        end = tuple(dim["end"])
        offset = dim["offset"]
        # Claude can't reliably know which offset sign points away from the
        # room (it depends on each wall's start->end direction), so dimension
        # lines sometimes land inside the plan, colliding with openings and
        # labels. Deterministically pick the sign that pushes the dimension
        # line away from the plan's center - always outward for perimeter
        # walls, and a harmless coin-flip for genuinely interior ones.
        if center is not None and offset:
            direction = _unit(_sub(end, start))
            perpendicular = _perp(direction)
            mid = _scale(_add(start, end), 0.5)
            cand_pos = _add(mid, _scale(perpendicular, abs(offset)))
            cand_neg = _sub(mid, _scale(perpendicular, abs(offset)))
            dist_pos = _length(_sub(cand_pos, center))
            dist_neg = _length(_sub(cand_neg, center))
            offset = abs(offset) if dist_pos >= dist_neg else -abs(offset)
        draw_measurement(
            msp, start, end, offset, "DIMS",
            text_height=180, gap=80, overshoot=80, tick_size=120,
        )


def build_doc(spec: dict):
    """Build and return an ezdxf Drawing object from a floor-plan spec dict.

    Does not write to disk.
    """
    doc = ezdxf.new(setup=True)
    doc.units = ezdxf.units.MM
    msp = doc.modelspace()

    for name, lineweight in LAYER_DEFS.items():
        layer = doc.layers.add(name, color=7)
        layer.dxf.lineweight = lineweight

    walls = spec.get("walls", []) or []
    openings = spec.get("openings", []) or []
    rooms = spec.get("rooms", []) or []
    dimensions = spec.get("dimensions", []) or []

    _draw_walls(msp, walls, openings)
    _draw_openings(msp, walls, openings)
    _draw_rooms(msp, rooms)
    _draw_dimensions(msp, dimensions, walls)

    return doc


# doc_to_dxf_bytes / doc_to_preview_bytes are imported from dxf_render (shared
# with container_engine.py) and re-exported here as this module's public API.
