"""
container_engine.py

Turns a JSON "container home spec" into an in-memory ezdxf Drawing
representing a multi-view shop-drawing sheet (plan + elevations), and
serializes that drawing to DXF bytes or a PNG preview.

Public interface (mirrors cad_engine.py so a Flask app can use either
engine polymorphically):

    build_doc(spec: dict) -> ezdxf Drawing
    doc_to_dxf_bytes(doc) -> bytes
    doc_to_preview_bytes(doc) -> bytes

All geometry is in millimeters. Nothing is written to disk.
"""

import ezdxf

from dxf_render import doc_to_dxf_bytes  # noqa: F401 - re-exported
from dxf_render import doc_to_preview_bytes as _doc_to_preview_bytes


ROW_GAP_MM = 1500
COL_GAP_MM = 1000


# ---------------------------------------------------------------------------
# Small drawing helpers
# ---------------------------------------------------------------------------

def _rect(msp, x0, y0, x1, y1, layer):
    points = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    msp.add_lwpolyline(points, close=True, dxfattribs={"layer": layer})


def _mtext_simple(msp, text, x, y, height, layer):
    """Place MTEXT with its insertion point roughly centered on (x, y)."""
    mt = msp.add_mtext(text, dxfattribs={"layer": layer, "char_height": height, "insert": (x, y)})
    try:
        mt.dxf.attachment_point = 5  # MIDDLE_CENTER
    except Exception:
        pass
    return mt


def _title_below(msp, title, cx, y, layer="TEXT", height=120):
    if title:
        _mtext_simple(msp, title, cx, y, height, layer)


def _dim_chain(msp, base_offset, points_y, xs, layer="DIMS"):
    """
    Draw a chain of consecutive linear dimensions along a horizontal run.

    xs: list of x coordinates defining the boundaries (n+1 values for n
        dimension segments).
    points_y: the y coordinate of the measured line (p1/p2).
    base_offset: the y coordinate of the dimension line itself.
    """
    for i in range(len(xs) - 1):
        x0 = xs[i]
        x1 = xs[i + 1]
        dim = msp.add_linear_dim(
            base=(x0, base_offset),
            p1=(x0, points_y),
            p2=(x1, points_y),
            dimstyle="EZDXF",
            dxfattribs={"layer": layer},
        )
        dim.render()


# ---------------------------------------------------------------------------
# Plan view
# ---------------------------------------------------------------------------

def draw_plan_view(msp, spec, origin):
    ox, oy = origin
    container = spec.get("container", {})
    length = container.get("length_mm", 6058)
    width = container.get("width_mm", 2438)

    _rect(msp, ox, oy, ox + length, oy + width, "OUTLINE")

    plan = spec.get("plan", {})

    kitchen_run = plan.get("kitchen_run")
    if kitchen_run:
        depth = kitchen_run.get("depth_mm", 700)
        segments = kitchen_run.get("segments", [])
        if segments:
            band_y0 = oy
            band_y1 = oy + depth
            cursor_x = ox
            boundary_xs = [cursor_x]

            for seg in segments:
                seg_width = seg.get("width_mm", 0)
                x_end = cursor_x + seg_width

                msp.add_line((x_end, band_y0), (x_end, band_y1),
                             dxfattribs={"layer": "KITCHEN"})

                label = seg.get("label", "")
                if label:
                    mid_x = cursor_x + seg_width / 2.0
                    mid_y = (band_y0 + band_y1) / 2.0
                    _mtext_simple(msp, label, mid_x, mid_y, 90, "KITCHEN")

                cursor_x = x_end
                boundary_xs.append(cursor_x)

            dim_line_y = band_y1 + 250
            _dim_chain(msp, dim_line_y, band_y1, boundary_xs, layer="DIMS")

    sliding_door = plan.get("sliding_door")
    if sliding_door:
        door_width = sliding_door.get("width_mm", 0)
        pos_from_left = sliding_door.get("position_from_left_mm", 0)
        door_x0 = ox + pos_from_left
        door_x1 = door_x0 + door_width

        msp.add_line((door_x0, oy), (door_x0, oy - 150), dxfattribs={"layer": "OUTLINE"})
        msp.add_line((door_x1, oy), (door_x1, oy - 150), dxfattribs={"layer": "OUTLINE"})
        msp.add_line((door_x0, oy - 75), (door_x1, oy - 75),
                      dxfattribs={"layer": "OUTLINE", "linetype": "DASHED"})

        far_end_x = ox + length
        boundary_xs = [ox, door_x0, door_x1, far_end_x]
        dim_line_y = oy - 400
        _dim_chain(msp, dim_line_y, oy - 150, boundary_xs, layer="DIMS")

    title = plan.get("title")
    _title_below(msp, title, ox + length / 2.0, oy - 700)


# ---------------------------------------------------------------------------
# Front elevation
# ---------------------------------------------------------------------------

def _hatch_diagonals(msp, x0, y0, x1, y1, layer, count=4):
    """Draw `count` parallel 45-degree lines across a panel rectangle,
    the standard drafting convention for glazing in elevation."""
    width = x1 - x0
    height = y1 - y0
    step = width / (count + 1.0)
    x_start = x0
    for i in range(1, count + 1):
        x_start = x0 + step * i
        x_end = x_start - height
        if x_end < x0:
            y_at_x0 = y0 + (x_start - x0)
            msp.add_line((x0, y_at_x0), (x_start, y0), dxfattribs={"layer": layer})
        else:
            msp.add_line((x_end, y1), (x_start, y0), dxfattribs={"layer": layer})


def draw_front_elevation(msp, spec, origin):
    ox, oy = origin
    container = spec.get("container", {})
    length = container.get("length_mm", 6058)

    front = spec.get("front_elevation", {})
    height = front.get("height_mm", container.get("height_mm", 2896))

    _rect(msp, ox, oy, ox + length, oy + height, "OUTLINE")

    panels = front.get("glazing_panels")
    boundary_xs = [ox]
    if panels:
        cursor_x = ox
        for panel in panels:
            pw = panel.get("width_mm", 0)
            x0 = cursor_x
            x1 = cursor_x + pw
            _rect(msp, x0, oy, x1, oy + height, "GLAZING")

            ptype = panel.get("type", "")
            if ptype == "sliding_glass":
                _hatch_diagonals(msp, x0, oy, x1, oy + height, "GLAZING", count=3)
            elif ptype == "frame":
                msp.add_line((x0, oy), (x1, oy + height), dxfattribs={"layer": "GLAZING"})
                msp.add_line((x0, oy + height), (x1, oy), dxfattribs={"layer": "GLAZING"})

            cursor_x = x1
            boundary_xs.append(cursor_x)

        dim_line_y = oy - 250
        _dim_chain(msp, dim_line_y, oy, boundary_xs, layer="DIMS")

    callouts = front.get("frame_callouts")
    if callouts:
        text_y = oy + height + 250
        for callout in callouts:
            _mtext_simple(msp, callout, ox + length / 2.0, text_y, 110, "CALLOUTS")
            text_y += 220

    if front.get("cable_bracing"):
        msp.add_line((ox, oy), (ox + length * 0.5, oy + height),
                     dxfattribs={"layer": "CALLOUTS"})
        _mtext_simple(msp, "cable brace", ox + length * 0.25, oy + height * 0.5,
                      100, "CALLOUTS")

    title = front.get("title")
    _title_below(msp, title, ox + length / 2.0, oy - 700)


# ---------------------------------------------------------------------------
# Side elevation
# ---------------------------------------------------------------------------

def draw_side_elevation(msp, spec, origin):
    ox, oy = origin
    container = spec.get("container", {})
    width = container.get("width_mm", 2438)
    height = container.get("height_mm", 2896)

    _rect(msp, ox, oy, ox + width, oy + height, "OUTLINE")

    side = spec.get("side_elevation", {})
    platform = side.get("fold_out_platform")
    if platform:
        pw = platform.get("width_mm", 0)
        radius = platform.get("swing_radius_mm", pw)

        pivot_x = ox + width
        pivot_y = oy

        _rect(msp, pivot_x, pivot_y, pivot_x + pw, pivot_y + 60, "CALLOUTS")

        # Arc sweeping from the deployed (horizontal, angle 0) position up to
        # the stowed (vertical, angle 90) position against the container wall.
        msp.add_arc(
            center=(pivot_x, pivot_y),
            radius=radius,
            start_angle=0,
            end_angle=90,
            dxfattribs={"layer": "CALLOUTS"},
        )

        label = side.get("floor_extension_label")
        if label:
            _mtext_simple(msp, label, pivot_x + pw / 2.0, pivot_y - 150, 100, "CALLOUTS")

    title = side.get("title")
    _title_below(msp, title, ox + width / 2.0, oy - 700)


# ---------------------------------------------------------------------------
# Back elevation
# ---------------------------------------------------------------------------

def draw_back_elevation(msp, spec, origin):
    ox, oy = origin
    container = spec.get("container", {})
    width = container.get("width_mm", 2438)
    height = container.get("height_mm", 2896)

    _rect(msp, ox, oy, ox + width, oy + height, "OUTLINE")

    spacing = 200
    x = ox + spacing
    while x < ox + width:
        msp.add_line((x, oy), (x, oy + height), dxfattribs={"layer": "CORRUGATION"})
        x += spacing

    back = spec.get("back_elevation", {})
    vent = back.get("vent_window")
    if vent:
        vw = vent.get("width_mm", 0)
        vh = vent.get("height_mm", 0)
        pos_from_left = vent.get("position_from_left_mm", 0)
        center_h = vent.get("center_height_mm", height / 2.0)

        x0 = ox + pos_from_left
        x1 = x0 + vw
        y0 = oy + center_h - vh / 2.0
        y1 = oy + center_h + vh / 2.0

        _rect(msp, x0, y0, x1, y1, "OUTLINE")

        dim_line_y = y1 + 250
        _dim_chain(msp, dim_line_y, y1, [x0, x1], layer="DIMS")

    title = back.get("title")
    _title_below(msp, title, ox + width / 2.0, oy - 700)


# ---------------------------------------------------------------------------
# Top-level builder
# ---------------------------------------------------------------------------

def _add_layers(doc):
    layer_defs = [
        ("OUTLINE", 7),
        ("KITCHEN", 3),
        ("GLAZING", 5),
        ("DIMS", 1),
        ("TEXT", 2),
        ("CORRUGATION", 8),
        ("CALLOUTS", 6),
    ]
    for name, color in layer_defs:
        if name not in doc.layers:
            doc.layers.add(name, color=color)


def build_doc(spec: dict):
    """Build and return an ezdxf Drawing object from a container-home spec dict.

    Does not write to disk.
    """
    doc = ezdxf.new(setup=True)
    msp = doc.modelspace()

    _add_layers(doc)

    container = spec.get("container", {})
    length = container.get("length_mm", 6058)
    height = container.get("height_mm", 2896)

    # BELOW_MARGIN_MM is a conservative allowance for the title text and any
    # dimension chains a view draws below its own base rectangle, so the next
    # row's origin can be computed without the two rows' geometry colliding.
    BELOW_MARGIN_MM = 1000

    plan_origin = (0, 0)
    draw_plan_view(msp, spec, plan_origin)

    # cursor_y is the y coordinate below which the next row's top edge must sit.
    cursor_y = 0 - BELOW_MARGIN_MM - ROW_GAP_MM

    front = spec.get("front_elevation", {})
    front_height = front.get("height_mm", height)
    front_origin_y = cursor_y - front_height
    front_origin = (0, front_origin_y)
    draw_front_elevation(msp, spec, front_origin)

    side_x = length + COL_GAP_MM
    side_origin = (side_x, front_origin_y)
    draw_side_elevation(msp, spec, side_origin)

    cursor_y = front_origin_y - BELOW_MARGIN_MM - ROW_GAP_MM

    back_origin_y = cursor_y - height
    back_origin = (0, back_origin_y)
    draw_back_elevation(msp, spec, back_origin)

    return doc


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def doc_to_preview_bytes(doc) -> bytes:
    """Render the drawing to a PNG image, in-memory, and return the PNG bytes."""
    return _doc_to_preview_bytes(doc, figsize=(12, 14))
