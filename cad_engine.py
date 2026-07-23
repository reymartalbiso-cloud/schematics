"""CAD-generation engine: turns a JSON floor-plan spec into an in-memory ezdxf
Drawing, and serializes that drawing to DXF bytes or a PNG preview image.

No disk writes anywhere in this module.
"""

import math

import ezdxf

from dxf_render import doc_to_dxf_bytes, doc_to_preview_bytes  # noqa: F401 - re-exported


LAYER_DEFS = {
    "WALLS": 7,
    "DOORS": 1,
    "WINDOWS": 5,
    "TEXT": 2,
    "DIMS": 3,
}


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1])


def _scale(v, s):
    return (v[0] * s, v[1] * s)


def _length(v):
    return math.hypot(v[0], v[1])


def _unit(v):
    length = _length(v)
    return (v[0] / length, v[1] / length)


def _perp(v):
    # Rotate 90 degrees counter-clockwise.
    return (-v[1], v[0])


def _wall_vectors(wall):
    start = tuple(wall["start"])
    end = tuple(wall["end"])
    direction = _unit(_sub(end, start))
    perpendicular = _perp(direction)
    return start, end, direction, perpendicular


def _draw_walls(msp, walls):
    for wall in walls:
        start, end, direction, perpendicular = _wall_vectors(wall)
        half = wall["thickness"] / 2.0
        offset = _scale(perpendicular, half)
        p1 = _add(start, offset)
        p2 = _add(end, offset)
        p3 = _sub(end, offset)
        p4 = _sub(start, offset)
        msp.add_lwpolyline(
            [p1, p2, p3, p4], close=True, dxfattribs={"layer": "WALLS"}
        )


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


def _draw_dimensions(msp, dimensions):
    for dim in dimensions:
        start = tuple(dim["start"])
        end = tuple(dim["end"])
        offset = dim["offset"]
        direction = _unit(_sub(end, start))
        perpendicular = _perp(direction)
        base = _add(start, _scale(perpendicular, offset))
        dim_entity = msp.add_linear_dim(
            base=base,
            p1=start,
            p2=end,
            dxfattribs={"layer": "DIMS"},
        )
        dim_entity.render()


def build_doc(spec: dict):
    """Build and return an ezdxf Drawing object from a floor-plan spec dict.

    Does not write to disk.
    """
    doc = ezdxf.new(setup=True)
    doc.units = ezdxf.units.MM
    msp = doc.modelspace()

    for name, color in LAYER_DEFS.items():
        doc.layers.add(name, color=color)

    walls = spec.get("walls", []) or []
    openings = spec.get("openings", []) or []
    rooms = spec.get("rooms", []) or []
    dimensions = spec.get("dimensions", []) or []

    _draw_walls(msp, walls)
    _draw_openings(msp, walls, openings)
    _draw_rooms(msp, rooms)
    _draw_dimensions(msp, dimensions)

    return doc


# doc_to_dxf_bytes / doc_to_preview_bytes are imported from dxf_render (shared
# with container_engine.py) and re-exported here as this module's public API.
