"""
container_engine.py

Turns a JSON "container home spec" into an in-memory ezdxf Drawing
representing a multi-view shop-drawing sheet (plan + elevations), and
serializes that drawing to DXF bytes or a PNG preview.

Public interface (mirrors cad_engine.py so a Flask app can use either
engine polymorphically):

    build_doc(spec: dict, views: list[str] | None = None) -> ezdxf Drawing
    doc_to_dxf_bytes(doc) -> bytes
    doc_to_preview_bytes(doc) -> bytes

`views` selects which sheets get drawn - defaults to plan-only. Valid
entries: "plan", "front", "side", "back". The front/side/back drawing
functions stay in this module regardless of what's requested by default,
so a caller can always ask for the full sheet.

All geometry is in millimeters. Nothing is written to disk.
"""

import ezdxf

from dxf_render import doc_to_dxf_bytes  # noqa: F401 - re-exported
from dxf_render import doc_to_preview_bytes as _doc_to_preview_bytes
from dxf_render import draw_measurement


ROW_GAP_MM = 1500
COL_GAP_MM = 1000

DEFAULT_VIEWS = ["plan"]
ALL_VIEWS = ["plan", "front", "side", "back"]

DEFAULT_WALL_THICKNESS_MM = 60

# Dimension text/arrow sizing, matching the neighborhood of the kitchen/
# callout label text (char_height 90-120) already used in this drawing.
# Dimensions are drawn by hand (see dxf_render.draw_measurement) rather than
# via ezdxf's DIMENSION entities - real-world testing found LibreCAD never
# displays DIMENSION text regardless of style/height configuration, even
# though the underlying DXF data is correct and standards-compliant. Plain
# MTEXT (what draw_measurement uses) renders identically everywhere.
DIM_TEXT_HEIGHT = 120
DIM_TICK_SIZE = 80
DIM_GAP = 50
DIM_OVERSHOOT = 50


def _dim(msp, p1, p2, offset, layer="DIMS", text=None):
    draw_measurement(
        msp, p1, p2, offset, layer,
        text=text,
        text_height=DIM_TEXT_HEIGHT, gap=DIM_GAP, overshoot=DIM_OVERSHOOT, tick_size=DIM_TICK_SIZE,
    )


# ---------------------------------------------------------------------------
# Small drawing helpers
# ---------------------------------------------------------------------------

def _rect(msp, x0, y0, x1, y1, layer):
    points = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    msp.add_lwpolyline(points, close=True, dxfattribs={"layer": layer})


def _rect_open_sides(msp, x0, y0, x1, y1, layer, bottom_gap=None):
    """Draw a rectangle as 4 independent line segments (rather than one
    closed polyline) so the bottom side can be interrupted by a door
    opening. `bottom_gap`, if given, is an (x0, x1) range left open."""
    msp.add_line((x0, y1), (x1, y1), dxfattribs={"layer": layer})  # top
    msp.add_line((x0, y0), (x0, y1), dxfattribs={"layer": layer})  # left
    msp.add_line((x1, y0), (x1, y1), dxfattribs={"layer": layer})  # right

    if bottom_gap is None:
        msp.add_line((x0, y0), (x1, y0), dxfattribs={"layer": layer})
        return

    gap_x0, gap_x1 = bottom_gap
    if x0 < gap_x0:
        msp.add_line((x0, y0), (gap_x0, y0), dxfattribs={"layer": layer})
    if gap_x1 < x1:
        msp.add_line((gap_x1, y0), (x1, y0), dxfattribs={"layer": layer})


def _insulation_fill(msp, x0, y0, x1, y1, layer="INSULATION", step=180):
    """Zigzag line filling a thin wall-cavity rectangle - the standard CAD
    drafting convention indicating insulation/panel fill inside a wall,
    rather than a plain outline."""
    width = x1 - x0
    height = y1 - y0
    if width <= 0 or height <= 0:
        return

    if width >= height:
        mid_y = (y0 + y1) / 2.0
        amp = height * 0.35
        n = max(2, int(round(width / step)))
        pts = [
            (x0 + width * i / n, mid_y + (amp if i % 2 == 0 else -amp))
            for i in range(n + 1)
        ]
    else:
        mid_x = (x0 + x1) / 2.0
        amp = width * 0.35
        n = max(2, int(round(height / step)))
        pts = [
            (mid_x + (amp if i % 2 == 0 else -amp), y0 + height * i / n)
            for i in range(n + 1)
        ]
    msp.add_lwpolyline(pts, dxfattribs={"layer": layer})


def _draw_container_walls(msp, ox, oy, length, width, thickness, door_gap=None):
    """Double-line walls with an insulation zigzag fill between the outer and
    inner face, matching standard architectural wall-cavity drafting rather
    than a plain single-line rectangle. `door_gap`, if given, is an (x0, x1)
    range on the bottom wall left open for a sliding-door track, drawn as a
    double parallel-line break rather than a single line or closed box.

    Returns the interior (room-side) rectangle bounds: (ix0, iy0, ix1, iy1).
    """
    ix0, iy0 = ox + thickness, oy + thickness
    ix1, iy1 = ox + length - thickness, oy + width - thickness

    _rect_open_sides(msp, ox, oy, ox + length, oy + width, "OUTLINE", bottom_gap=door_gap)
    _rect_open_sides(msp, ix0, iy0, ix1, iy1, "OUTLINE", bottom_gap=door_gap)

    # Left, right, and top wall cavities always run their full extent.
    _insulation_fill(msp, ox, oy, ix0, oy + width)
    _insulation_fill(msp, ix1, oy, ox + length, oy + width)
    _insulation_fill(msp, ox, iy1, ox + length, oy + width)

    if door_gap is None:
        _insulation_fill(msp, ox, oy, ox + length, iy0)
    else:
        gap_x0, gap_x1 = door_gap
        if ox < gap_x0:
            _insulation_fill(msp, ox, oy, gap_x0, iy0)
        if gap_x1 < ox + length:
            _insulation_fill(msp, gap_x1, oy, ox + length, iy0)

        # Double parallel lines across the gap = sliding-door track, the
        # standard convention for a sliding opening rather than a hinged door.
        track_y1 = oy + thickness * 0.33
        track_y2 = oy + thickness * 0.66
        msp.add_line((gap_x0, track_y1), (gap_x1, track_y1), dxfattribs={"layer": "DOOR"})
        msp.add_line((gap_x0, track_y2), (gap_x1, track_y2), dxfattribs={"layer": "DOOR"})

    return ix0, iy0, ix1, iy1


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
    Draw a chain of consecutive horizontal dimensions along a run.

    xs: list of x coordinates defining the boundaries (n+1 values for n
        dimension segments).
    points_y: the y coordinate of the measured line (p1/p2).
    base_offset: the y coordinate of the dimension line itself.
    """
    for i in range(len(xs) - 1):
        _dim(msp, (xs[i], points_y), (xs[i + 1], points_y), base_offset - points_y, layer)


def _dim_chain_vertical(msp, base_offset, points_x, ys, layer="DIMS"):
    """Vertical counterpart to _dim_chain - a chain of stacked dimensions
    along a run measured in the y direction.

    Note the offset sign is points_x - base_offset, not base_offset -
    points_x: perp() rotates 90 degrees CCW, so an ascending-y direction's
    perpendicular points in -x, the opposite sense from an ascending-x
    direction's perpendicular (+y) used by _dim_chain.
    """
    for i in range(len(ys) - 1):
        _dim(msp, (points_x, ys[i]), (points_x, ys[i + 1]), points_x - base_offset, layer)


# ---------------------------------------------------------------------------
# Kitchen fixture symbols
# ---------------------------------------------------------------------------

_STOVE_KEYWORDS = ("stove", "hob", "cook")
_SINK_KEYWORDS = ("sink", "basin")
_FRIDGE_KEYWORDS = ("fridge", "ref", "refrigerator")


def _classify_fixture(label):
    text = (label or "").strip().lower()
    if any(k in text for k in _STOVE_KEYWORDS):
        return "stove"
    if any(k in text for k in _SINK_KEYWORDS):
        return "sink"
    if any(k in text for k in _FRIDGE_KEYWORDS):
        return "fridge"
    return None


def _draw_stove_symbol(msp, x0, y0, x1, y1, layer):
    """2x2 grid of small circles representing burners, the standard plan
    symbol for a stove/hob - not just a text label."""
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    span_x = (x1 - x0) * 0.3
    span_y = (y1 - y0) * 0.3
    radius = min(span_x, span_y) * 0.35
    for dx in (-span_x / 2.0, span_x / 2.0):
        for dy in (-span_y / 2.0, span_y / 2.0):
            msp.add_circle(center=(cx + dx, cy + dy), radius=radius, dxfattribs={"layer": layer})


def _rounded_rect(msp, x0, y0, x1, y1, radius, layer):
    """Rounded-corner rectangle built from 4 lines + 4 arcs - used for the
    sink basin symbols."""
    r = max(1.0, min(radius, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    msp.add_line((x0 + r, y0), (x1 - r, y0), dxfattribs={"layer": layer})
    msp.add_line((x1, y0 + r), (x1, y1 - r), dxfattribs={"layer": layer})
    msp.add_line((x1 - r, y1), (x0 + r, y1), dxfattribs={"layer": layer})
    msp.add_line((x0, y1 - r), (x0, y0 + r), dxfattribs={"layer": layer})
    msp.add_arc(center=(x1 - r, y0 + r), radius=r, start_angle=-90, end_angle=0, dxfattribs={"layer": layer})
    msp.add_arc(center=(x1 - r, y1 - r), radius=r, start_angle=0, end_angle=90, dxfattribs={"layer": layer})
    msp.add_arc(center=(x0 + r, y1 - r), radius=r, start_angle=90, end_angle=180, dxfattribs={"layer": layer})
    msp.add_arc(center=(x0 + r, y0 + r), radius=r, start_angle=180, end_angle=270, dxfattribs={"layer": layer})


def _draw_sink_symbol(msp, x0, y0, x1, y1, layer, dim_layer):
    """Two side-by-side rounded basins with a faucet tick and a local
    dimension measuring the spacing across them - not just a text label."""
    margin_x = (x1 - x0) * 0.12
    margin_y = (y1 - y0) * 0.2
    inner_x0, inner_x1 = x0 + margin_x, x1 - margin_x
    inner_y0, inner_y1 = y0 + margin_y, y1 - margin_y
    mid_x = (inner_x0 + inner_x1) / 2.0
    gap = (inner_x1 - inner_x0) * 0.08
    basin_w = (inner_x1 - inner_x0 - gap) / 2.0
    radius = min(basin_w, inner_y1 - inner_y0) * 0.18

    left_x0, left_x1 = inner_x0, inner_x0 + basin_w
    right_x0, right_x1 = left_x1 + gap, inner_x1
    _rounded_rect(msp, left_x0, inner_y0, left_x1, inner_y1, radius, layer)
    _rounded_rect(msp, right_x0, inner_y0, right_x1, inner_y1, radius, layer)

    tick_y0 = inner_y1
    tick_y1 = inner_y1 + (inner_y1 - inner_y0) * 0.25
    msp.add_line((mid_x, tick_y0), (mid_x, tick_y1), dxfattribs={"layer": layer})

    dim_offset = (inner_y1 - inner_y0) * 0.4
    _dim(msp, (left_x0, tick_y1), (right_x1, tick_y1), dim_offset, dim_layer)


def _draw_fridge_symbol(msp, x0, y0, x1, y1, depth, layer, dim_layer):
    """Fridge footprint with a door-swing/handle indicator and a stacked
    pair of local depth-detail dimensions - not just a text label."""
    hinge_radius = min(x1 - x0, y1 - y0) * 0.5
    msp.add_arc(
        center=(x1, y0), radius=hinge_radius,
        start_angle=90, end_angle=180,
        dxfattribs={"layer": layer},
    )

    detail_x1 = x1 + 120
    detail_x2 = x1 + 280
    dim_a = max(5.0, round(depth * 0.5 / 5.0) * 5)
    dim_b = max(5.0, round(depth * 0.79 / 5.0) * 5)

    # direction here is ascending-y ((0,1)), whose perpendicular points in
    # -x (see _dim_chain_vertical) - offset sign is x1 - detail_x, matching.
    _dim(msp, (x1, y0), (x1, y0 + dim_a), x1 - detail_x1, dim_layer)
    _dim(msp, (x1, y0), (x1, y0 + dim_b), x1 - detail_x2, dim_layer)


# ---------------------------------------------------------------------------
# Plan view
# ---------------------------------------------------------------------------

def draw_plan_view(msp, spec, origin):
    ox, oy = origin
    container = spec.get("container", {})
    length = container.get("length_mm", 6058)
    width = container.get("width_mm", 2438)
    thickness = container.get("wall_thickness_mm", DEFAULT_WALL_THICKNESS_MM)

    plan = spec.get("plan", {})
    kitchen_run = plan.get("kitchen_run")
    sliding_door = plan.get("sliding_door")

    door_gap = None
    if sliding_door:
        door_width = sliding_door.get("width_mm", 0)
        pos_from_left = sliding_door.get("position_from_left_mm", 0)
        door_gap = (ox + pos_from_left, ox + pos_from_left + door_width)

    ix0, iy0, ix1, iy1 = _draw_container_walls(msp, ox, oy, length, width, thickness, door_gap)

    bottom_dim_y = oy  # tracks how far dimension rows have stacked below the wall

    if kitchen_run:
        depth = kitchen_run.get("depth_mm", 700)
        segments = kitchen_run.get("segments", [])
        if segments:
            band_y0 = iy0
            band_y1 = band_y0 + depth
            cursor_x = ix0
            boundary_xs = [cursor_x]

            for seg in segments:
                seg_width = seg.get("width_mm", 0)
                seg_x0, seg_x1 = cursor_x, cursor_x + seg_width

                msp.add_line((seg_x1, band_y0), (seg_x1, band_y1), dxfattribs={"layer": "KITCHEN"})

                kind = _classify_fixture(seg.get("label", ""))
                if kind == "stove":
                    _draw_stove_symbol(msp, seg_x0, band_y0, seg_x1, band_y1, "KITCHEN")
                elif kind == "sink":
                    _draw_sink_symbol(msp, seg_x0, band_y0, seg_x1, band_y1, "KITCHEN", "DIMS")
                elif kind == "fridge":
                    _draw_fridge_symbol(msp, seg_x0, band_y0, seg_x1, band_y1, depth, "KITCHEN", "DIMS")

                label = seg.get("label", "")
                if label:
                    mid_x = (seg_x0 + seg_x1) / 2.0
                    label_y = band_y0 + min(120.0, depth * 0.2)
                    _mtext_simple(msp, label, mid_x, label_y, 90, "KITCHEN")

                cursor_x = seg_x1
                boundary_xs.append(cursor_x)

            # Front edge of the counter, facing into the room - without this
            # the counter reads as bare divider lines rather than a real run.
            msp.add_line((ix0, band_y1), (cursor_x, band_y1), dxfattribs={"layer": "KITCHEN"})

            row1_y = band_y1 + 250
            _dim_chain(msp, row1_y, band_y1, boundary_xs, layer="DIMS")

    if sliding_door:
        door_x0, door_x1 = door_gap
        far_end_x = ox + length
        boundary_xs = [ox, door_x0, door_x1, far_end_x]
        row2_y = oy - 400
        _dim_chain(msp, row2_y, oy, boundary_xs, layer="DIMS")
        bottom_dim_y = row2_y

    # Outermost overall span for this wall (interior face to interior face),
    # always shown regardless of kitchen/door fixtures.
    row3_y = bottom_dim_y - 500
    _dim_chain(msp, row3_y, oy, [ix0, ix1], layer="DIMS")
    bottom_dim_y = row3_y

    # Outermost dimensions of the whole drawing: container overall length
    # across the top, overall width down the side.
    _dim_chain(msp, oy + width + 400, oy + width, [ox, ox + length], layer="DIMS")
    _dim_chain_vertical(msp, ox - 400, ox, [oy, oy + width], layer="DIMS")

    title = plan.get("title")
    _title_below(msp, title, ox + length / 2.0, bottom_dim_y - 400)


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
        ("INSULATION", 8),
        ("DOOR", 1),
    ]
    for name, color in layer_defs:
        if name not in doc.layers:
            doc.layers.add(name, color=color)


def build_doc(spec: dict, views: list | None = None):
    """Build and return an ezdxf Drawing object from a container-home spec dict.

    `views` selects which sheets to draw (subset of "plan"/"front"/"side"/
    "back"); defaults to plan-only. Does not write to disk.
    """
    doc = ezdxf.new(setup=True)
    # Without this, dimension text renders scaled 100x too large (e.g. 605800
    # instead of 6058) - ezdxf's automatic dimension measurement depends on
    # the document's declared drawing units, which default to unitless.
    doc.units = ezdxf.units.MM
    msp = doc.modelspace()

    _add_layers(doc)

    requested = set(views) if views else set(DEFAULT_VIEWS)

    container = spec.get("container", {})
    length = container.get("length_mm", 6058)
    height = container.get("height_mm", 2896)

    # BELOW_MARGIN_MM is a conservative allowance for the title text and any
    # dimension chains a view draws below its own base rectangle, so the next
    # row's origin can be computed without the two rows' geometry colliding.
    BELOW_MARGIN_MM = 1000

    cursor_y = 0

    if "plan" in requested:
        draw_plan_view(msp, spec, (0, cursor_y))
        cursor_y = cursor_y - BELOW_MARGIN_MM - ROW_GAP_MM

    if "front" in requested or "side" in requested:
        front = spec.get("front_elevation", {})
        front_height = front.get("height_mm", height)
        front_origin_y = cursor_y - front_height

        if "front" in requested:
            draw_front_elevation(msp, spec, (0, front_origin_y))
        if "side" in requested:
            side_x = length + COL_GAP_MM
            draw_side_elevation(msp, spec, (side_x, front_origin_y))

        cursor_y = front_origin_y - BELOW_MARGIN_MM - ROW_GAP_MM

    if "back" in requested:
        back_origin_y = cursor_y - height
        draw_back_elevation(msp, spec, (0, back_origin_y))

    return doc


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def doc_to_preview_bytes(doc) -> bytes:
    """Render the drawing to a PNG image, in-memory, and return the PNG bytes."""
    return _doc_to_preview_bytes(doc, figsize=(12, 14))
