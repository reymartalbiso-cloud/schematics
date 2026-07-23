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


def _dim(msp, p1, p2, offset, layer="DIMS", text=None, text_shift=0):
    draw_measurement(
        msp, p1, p2, offset, layer,
        text=text, text_shift=text_shift,
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


def _draw_container_walls(msp, ox, oy, length, width, thickness, glass_front=False, back_gaps=None):
    """Insulated double-line walls (zigzag cavity fill) on the left, right,
    and back (top) sides, matching standard wall-cavity drafting. The front
    (bottom) side is either a fourth insulated wall or - when `glass_front`
    is set, as in the reference fold-open units - a thin glazing-frame wall
    drawn separately by the caller. `back_gaps` is a list of (x0, x1) window
    openings in the back wall: the cavity fill is interrupted across them so
    the window symbol isn't buried under insulation texture.

    Returns the interior (room-side) rectangle bounds: (ix0, iy0, ix1, iy1).
    """
    ix0, iy0 = ox + thickness, oy + thickness
    ix1, iy1 = ox + length - thickness, oy + width - thickness

    # The outer outline is always fully closed - the container envelope is
    # exactly what the overall dimensions measure. With a glass front, the
    # front wall band (between oy and iy0) is drawn by the caller as a
    # glazing frame instead of an insulated cavity, INSIDE the envelope.
    _rect_open_sides(msp, ox, oy, ox + length, oy + width, "OUTLINE")
    inner_gap = (ox, ox + length) if glass_front else None
    _rect_open_sides(msp, ix0, iy0, ix1, iy1, "OUTLINE", bottom_gap=inner_gap)

    # Left and right wall cavities always run their full extent.
    _insulation_fill(msp, ox, oy, ix0, oy + width)
    _insulation_fill(msp, ix1, oy, ox + length, oy + width)

    # Back (top) wall cavity, interrupted at window openings.
    cursor = ox
    for gap_x0, gap_x1 in sorted(back_gaps or []):
        if cursor < gap_x0:
            _insulation_fill(msp, cursor, iy1, gap_x0, oy + width)
        cursor = max(cursor, gap_x1)
    if cursor < ox + length:
        _insulation_fill(msp, cursor, iy1, ox + length, oy + width)

    if not glass_front:
        _insulation_fill(msp, ox, oy, ox + length, iy0)

    return ix0, iy0, ix1, iy1


def _front_frame_joints(ox, length, wall_t, doors, partition_faces=(), gap=50):
    """X positions of the front-wall breakdown joints, anchored at the
    EXTERIOR faces so the chain always sums to the container's overall
    length (wall / panel / 50 gap / door / 50 gap / ... / wall), with
    partition-wall faces folded in when a bathroom exists. Extension lines
    therefore always land on drawn geometry, and the bottom chain can never
    contradict the top overall dimension."""
    x0, x1 = ox, ox + length
    joints = {x0, x0 + wall_t, x1 - wall_t, x1}
    for dx0, dx1 in doors:
        joints.update((dx0 - gap, dx0, dx1, dx1 + gap))
    joints.update(partition_faces)
    return sorted(j for j in joints if x0 <= j <= x1)


def _mtext_simple(msp, text, x, y, height, layer):
    """Place MTEXT with its insertion point roughly centered on (x, y)."""
    mt = msp.add_mtext(text, dxfattribs={"layer": layer, "char_height": height, "insert": (x, y)})
    try:
        mt.dxf.attachment_point = 5  # MIDDLE_CENTER
    except Exception:
        pass
    return mt


def _title_below(msp, title, cx, y, layer="TEXT", height=130, scale="1:30"):
    """Underlined view title with a scale suffix, matching the reference
    convention ("Plan View  1:30" with the title underlined)."""
    if not title:
        return
    _mtext_simple(msp, f"{title}  {scale}", cx, y, height, layer)
    # Underline sized to the title text (~0.62 * char height per character
    # is a serviceable width estimate for the condensed CAD face).
    half_w = len(title) * height * 0.62 / 2.0
    shift = len(scale) * height * 0.31  # keep the underline under the title only
    msp.add_line(
        (cx - half_w - shift, y - height * 0.85),
        (cx + half_w - shift, y - height * 0.85),
        dxfattribs={"layer": layer},
    )


def _dim_chain(msp, base_offset, points_y, xs, layer="DIMS"):
    """
    Draw a chain of consecutive horizontal dimensions along a run.

    xs: list of x coordinates defining the boundaries (n+1 values for n
        dimension segments).
    points_y: the y coordinate of the measured line (p1/p2).
    base_offset: the y coordinate of the dimension line itself.

    Segments too narrow to hold their own label get their text staggered
    upward in alternation, so two adjacent tiny segments (e.g. a 50 gap
    next to a 60 wall) don't overprint into an illegible blob.
    """
    prev_narrow = False
    for i in range(len(xs) - 1):
        seg_w = xs[i + 1] - xs[i]
        label_w = len(str(round(seg_w))) * DIM_TEXT_HEIGHT * 0.62
        if seg_w < label_w * 1.3:
            # Narrow segment: always lift the label to a raised row so it
            # clears the baseline text of wider neighbors; two consecutive
            # narrow labels alternate between two raised rows so they never
            # merge with each other either.
            shift = DIM_TEXT_HEIGHT * (2.4 if prev_narrow else 1.2)
            prev_narrow = not prev_narrow
        else:
            shift = 0
            prev_narrow = False
        _dim(msp, (xs[i], points_y), (xs[i + 1], points_y), base_offset - points_y, layer,
             text_shift=shift)


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
    """2x2 grid of double-ringed circles representing burners, matching the
    reference drawings' hob symbol - not just a text label."""
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    span_x = (x1 - x0) * 0.42
    span_y = (y1 - y0) * 0.42
    radius = min(span_x, span_y) * 0.42
    for dx in (-span_x / 2.0, span_x / 2.0):
        for dy in (-span_y / 2.0, span_y / 2.0):
            center = (cx + dx, cy + dy)
            msp.add_circle(center=center, radius=radius, dxfattribs={"layer": layer})
            msp.add_circle(center=center, radius=radius * 0.55, dxfattribs={"layer": layer})


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


def _draw_sink_symbol(msp, x0, y0, x1, y1, layer):
    """Two side-by-side rounded basins with a faucet (circle + spout),
    matching the reference drawings' double-sink symbol."""
    margin_x = (x1 - x0) * 0.12
    # Extra clearance at the top (wall side) keeps the basins out of the
    # window width dimension drawn just under the back wall.
    inner_x0, inner_x1 = x0 + margin_x, x1 - margin_x
    inner_y0 = y0 + (y1 - y0) * 0.12
    inner_y1 = y1 - (y1 - y0) * 0.34
    mid_x = (inner_x0 + inner_x1) / 2.0
    gap = (inner_x1 - inner_x0) * 0.08
    basin_w = (inner_x1 - inner_x0 - gap) / 2.0
    radius = min(basin_w, inner_y1 - inner_y0) * 0.18

    left_x0, left_x1 = inner_x0, inner_x0 + basin_w
    right_x0, right_x1 = left_x1 + gap, inner_x1
    _rounded_rect(msp, left_x0, inner_y0, left_x1, inner_y1, radius, layer)
    _rounded_rect(msp, right_x0, inner_y0, right_x1, inner_y1, radius, layer)

    # Faucet between the basins along the back edge: small body circle with
    # a short spout line angled over one basin.
    faucet_r = (inner_y1 - inner_y0) * 0.12
    faucet_y = inner_y1 - faucet_r
    msp.add_circle(center=(mid_x, faucet_y), radius=faucet_r, dxfattribs={"layer": layer})
    msp.add_line(
        (mid_x, faucet_y),
        (mid_x - faucet_r * 2.2, faucet_y - faucet_r * 2.2),
        dxfattribs={"layer": layer},
    )


def _draw_fridge_symbol(msp, x0, y0, x1, y1, layer, text_layer):
    """Boxed fridge footprint labeled REF with a door line along the front
    edge, matching the reference drawings."""
    margin = min(x1 - x0, y1 - y0) * 0.08
    bx0, by0, bx1, by1 = x0 + margin, y0 + margin, x1 - margin, y1 - margin
    _rect(msp, bx0, by0, bx1, by1, layer)
    # Door leaf line along the room-facing edge with a small handle tick.
    msp.add_line((bx0, by0 + margin), (bx1, by0 + margin), dxfattribs={"layer": layer})
    msp.add_line(
        (bx1 - margin, by0 + margin), (bx1 - margin, by0), dxfattribs={"layer": layer}
    )
    _mtext_simple(msp, "REF", (bx0 + bx1) / 2.0, (by0 + by1) / 2.0, 110, text_layer)


# ---------------------------------------------------------------------------
# Bathroom (partition + fixture symbols, per the reference packages)
# ---------------------------------------------------------------------------

def _draw_toilet(msp, cx, wall_y, layer):
    """Toilet in plan: cistern against the back wall + bowl ellipse facing
    into the room (downward from wall_y)."""
    tank_w, tank_d = 420, 130
    _rect(msp, cx - tank_w / 2.0, wall_y - tank_d, cx + tank_w / 2.0, wall_y, layer)
    bowl_len, bowl_w = 430, 350
    msp.add_ellipse(
        center=(cx, wall_y - tank_d - bowl_len / 2.0),
        major_axis=(0, bowl_len / 2.0),
        ratio=bowl_w / bowl_len,
        dxfattribs={"layer": layer},
    )


def _draw_toilet_side(msp, wall_x, cy, into_room_sign, layer):
    """Toilet mounted on a side (end) wall: cistern against wall_x, bowl
    pointing into the room along into_room_sign (+1 = +x, -1 = -x)."""
    tank_w, tank_d = 420, 130
    _rect(msp, min(wall_x, wall_x + into_room_sign * tank_d), cy - tank_w / 2.0,
          max(wall_x, wall_x + into_room_sign * tank_d), cy + tank_w / 2.0, layer)
    bowl_len, bowl_w = 430, 350
    msp.add_ellipse(
        center=(wall_x + into_room_sign * (tank_d + bowl_len / 2.0), cy),
        major_axis=(bowl_len / 2.0, 0),
        ratio=bowl_w / bowl_len,
        dxfattribs={"layer": layer},
    )


def _draw_shower(msp, x0, y0, x1, y1, layer):
    """Shower tray: square enclosure with corner-to-corner diagonals and a
    center drain circle."""
    _rect(msp, x0, y0, x1, y1, layer)
    msp.add_line((x0, y0), (x1, y1), dxfattribs={"layer": layer})
    msp.add_line((x0, y1), (x1, y0), dxfattribs={"layer": layer})
    msp.add_circle(center=((x0 + x1) / 2.0, (y0 + y1) / 2.0), radius=40, dxfattribs={"layer": layer})


def _draw_basin(msp, x0, y0, x1, y1, layer):
    """Basin: small counter rectangle with an oval bowl inside."""
    _rect(msp, x0, y0, x1, y1, layer)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    major = (x1 - x0) * 0.36
    ratio = min(1.0, ((y1 - y0) * 0.32) / major)
    msp.add_ellipse(center=(cx, cy), major_axis=(major, 0), ratio=ratio, dxfattribs={"layer": layer})


def _draw_bathroom(msp, bathroom, ix0, ix1, y_front, y_back, wall_t=60):
    """Enclosed bathroom at one container end: insulated partition wall with
    a swing door near the front, and fixture symbols (shower in the back
    corner, toilet on the end wall when the room is wide enough - matching
    "on the wall" phrasing - else against the back wall, basin above the
    door swing).

    Returns both partition faces (x0, x1) for the bottom dimension chain.
    """
    width = bathroom.get("width_mm", 1200)
    right = bathroom.get("position", "right") != "left"
    fixtures = [str(f).lower() for f in (bathroom.get("fixtures") or ["toilet", "shower", "basin"])]

    if right:
        bx0, bx1 = ix1 - width, ix1
        px0, px1 = bx0 - wall_t, bx0
    else:
        bx0, bx1 = ix0, ix0 + width
        px0, px1 = bx1, bx1 + wall_t

    # Partition wall with a door opening near the front.
    door_w = max(600, min(750, (y_back - y_front) * 0.4))
    gap_y0 = y_front + 150
    gap_y1 = gap_y0 + door_w
    for x in (px0, px1):
        msp.add_line((x, y_front), (x, gap_y0), dxfattribs={"layer": "OUTLINE"})
        msp.add_line((x, gap_y1), (x, y_back), dxfattribs={"layer": "OUTLINE"})
    msp.add_line((px0, gap_y0), (px1, gap_y0), dxfattribs={"layer": "OUTLINE"})
    msp.add_line((px0, gap_y1), (px1, gap_y1), dxfattribs={"layer": "OUTLINE"})
    _insulation_fill(msp, px0, y_front, px1, gap_y0)
    _insulation_fill(msp, px0, gap_y1, px1, y_back)

    # Door leaf swinging into the bathroom, hinged at the opening's back edge.
    if right:
        hinge = (px1, gap_y1)
        leaf_tip = (px1 + door_w, gap_y1)
        start_angle, end_angle = 270, 360
    else:
        hinge = (px0, gap_y1)
        leaf_tip = (px0 - door_w, gap_y1)
        start_angle, end_angle = 180, 270
    msp.add_line(hinge, leaf_tip, dxfattribs={"layer": "DOOR"})
    msp.add_arc(center=hinge, radius=door_w, start_angle=start_angle, end_angle=end_angle,
                dxfattribs={"layer": "DOOR"})

    # Fixture layout (matches the reference container bathrooms and honors
    # "toilet on the right/left-hand wall"): the TOILET takes the end wall,
    # the SHOWER the partition-side back corner, the BASIN the partition-side
    # front. The end wall is the container end; the partition side is the
    # interior wall dividing the bathroom from the rest of the home.
    s = max(500, min(850, width * 0.5, (y_back - y_front) * 0.42))
    end_wall_x = bx1 if right else bx0
    into_room = -1 if right else 1
    # partition-side back-corner shower rectangle
    sh = (bx0, y_back - s, bx0 + s, y_back) if right else (bx1 - s, y_back - s, bx1, y_back)

    if "shower" in fixtures:
        _draw_shower(msp, *sh, "KITCHEN")

    if "basin" in fixtures:
        bw, bd = min(460, width - 200), 360
        by0 = gap_y1 + 120
        # Front of the room, partition side, clear of the back-corner shower.
        top_limit = (y_back - s - 80) if "shower" in fixtures else (y_back - 80)
        if bw > 0 and by0 + bd <= top_limit:
            if right:
                _draw_basin(msp, bx0, by0, bx0 + bw, by0 + bd, "KITCHEN")
            else:
                _draw_basin(msp, bx1 - bw, by0, bx1, by0 + bd, "KITCHEN")

    if "toilet" in fixtures:
        # End wall by default (bowl projects into the room), centered in the
        # clear vertical span. Narrow rooms fall back to the back wall in the
        # stretch the shower didn't take.
        toilet_on_end = width >= 900 and (y_back - gap_y1) >= 700
        if toilet_on_end:
            _draw_toilet_side(msp, end_wall_x, (gap_y1 + y_back) / 2.0, into_room, "KITCHEN")
        else:
            if "shower" in fixtures:
                span = (bx0 + s, bx1) if right else (bx0, bx1 - s)
            else:
                span = (bx0, bx1)
            if span[1] - span[0] >= 450:
                _draw_toilet(msp, (span[0] + span[1]) / 2.0, y_back, "KITCHEN")

    return (px0, px1)


# ---------------------------------------------------------------------------
# Plan view
# ---------------------------------------------------------------------------

def draw_plan_view(msp, spec, origin):
    """Plan view following the reference shop-drawing layout: kitchen run
    along the back (top) wall with symbol fixtures, W:/D: callouts, window
    breaks in the back wall, a glazing-frame front wall with sliding door,
    and the fold-out deck below with dashed projection lines and stacked
    dimension rows."""
    ox, oy = origin
    container = spec.get("container", {})
    length = container.get("length_mm", 6058)
    width = container.get("width_mm", 2438)
    thickness = container.get("wall_thickness_mm", DEFAULT_WALL_THICKNESS_MM)

    plan = spec.get("plan", {})
    kitchen_run = plan.get("kitchen_run")
    windows = plan.get("windows") or []
    deck = plan.get("deck")
    bathroom = plan.get("bathroom")

    # One or several sliding doors: plan.sliding_door (single) and
    # plan.sliding_doors (array) merge into one sorted list.
    door_specs = []
    single = plan.get("sliding_door")
    if single:
        door_specs.append(single)
    door_specs.extend(plan.get("sliding_doors") or [])
    doors = []
    for d in door_specs:
        d_x0 = ox + d.get("position_from_left_mm", 0)
        doors.append((d_x0, d_x0 + d.get("width_mm", 0), d))
    doors.sort(key=lambda t: t[0])

    back_gaps = []
    for win in windows:
        w_x0 = ox + win.get("position_from_left_mm", 0)
        back_gaps.append((w_x0, w_x0 + win.get("width_mm", 0)))

    ix0, iy0, ix1, iy1 = _draw_container_walls(
        msp, ox, oy, length, width, thickness,
        glass_front=bool(doors), back_gaps=back_gaps,
    )

    # --- Back (top) wall windows: jambs, slider panes, dim, W: callout ---
    # Width dim and callout stack ABOVE the wall (the kitchen band sits
    # directly beneath it inside, so an interior dim would strike through
    # the fixtures), with the overall length dim moved a row further out.
    for win, (w_x0, w_x1) in zip(windows, back_gaps):
        msp.add_line((w_x0, iy1), (w_x0, oy + width), dxfattribs={"layer": "OUTLINE"})
        msp.add_line((w_x1, iy1), (w_x1, oy + width), dxfattribs={"layer": "OUTLINE"})
        # Sliding-window symbol: two overlapping panes offset within the
        # cavity, with a meeting rail at the overlap.
        mid_x = (w_x0 + w_x1) / 2.0
        y_lo = iy1 + thickness * 0.3
        y_hi = iy1 + thickness * 0.7
        msp.add_line((w_x0, y_hi), (mid_x + (w_x1 - w_x0) * 0.05, y_hi), dxfattribs={"layer": "GLAZING"})
        msp.add_line((mid_x - (w_x1 - w_x0) * 0.05, y_lo), (w_x1, y_lo), dxfattribs={"layer": "GLAZING"})
        _dim(msp, (w_x0, oy + width), (w_x1, oy + width), 150, "DIMS")
        callout = f"W:{round(win.get('width_mm', 0))}*{round(win.get('height_mm', 0))}mm"
        _mtext_simple(msp, callout, (w_x0 + w_x1) / 2.0, oy + width + 400, 110, "TEXT")

    # --- Bathroom at one end (partition + swing door + fixture symbols) ---
    partition_faces = ()
    kitchen_x0 = ix0
    kitchen_x1 = ix1
    if bathroom:
        y_front = oy if doors else iy0
        partition_faces = _draw_bathroom(msp, bathroom, ix0, ix1, y_front, iy1, thickness)
        # Keep the kitchen run out of the bathroom's floor area.
        if bathroom.get("position", "right") == "left":
            kitchen_x0 = ix0 + bathroom.get("width_mm", 1200) + thickness
        else:
            kitchen_x1 = ix1 - bathroom.get("width_mm", 1200) - thickness

    # --- Kitchen run along the back (top) wall ---
    if kitchen_run:
        depth = kitchen_run.get("depth_mm", 700)
        segments = kitchen_run.get("segments", [])
        if segments:
            band_y1 = iy1
            band_y0 = band_y1 - depth
            mid_y = (band_y0 + band_y1) / 2.0
            cursor_x = kitchen_x0
            boundary_xs = [cursor_x]
            centerline_end = None  # trims the dashed midline before a trailing fridge

            for i, seg in enumerate(segments):
                seg_width = seg.get("width_mm", 0)
                seg_x0, seg_x1 = cursor_x, cursor_x + seg_width

                msp.add_line((seg_x1, band_y0), (seg_x1, band_y1), dxfattribs={"layer": "KITCHEN"})

                kind = _classify_fixture(seg.get("label", ""))
                if kind == "stove":
                    _draw_stove_symbol(msp, seg_x0, band_y0, seg_x1, band_y1, "KITCHEN")
                elif kind == "sink":
                    _draw_sink_symbol(msp, seg_x0, band_y0, seg_x1, band_y1, "KITCHEN")
                elif kind == "fridge":
                    _draw_fridge_symbol(msp, seg_x0, band_y0, seg_x1, band_y1, "KITCHEN", "TEXT")
                    if i == len(segments) - 1:
                        centerline_end = seg_x0
                else:
                    # Reference drawings label only the fridge (REF); plain
                    # counter/cabinet segments carry no text. Label anything
                    # that isn't a recognized symbol so no info is lost -
                    # placed below the dashed midline, not through it.
                    label = seg.get("label", "")
                    if label:
                        _mtext_simple(msp, label, (seg_x0 + seg_x1) / 2.0,
                                      (band_y0 + mid_y) / 2.0, 90, "KITCHEN")

                cursor_x = seg_x1
                boundary_xs.append(cursor_x)

            # Counter front edge facing the room, plus the dashed midline the
            # references use to mark the counter's centerline.
            msp.add_line((kitchen_x0, band_y0), (cursor_x, band_y0), dxfattribs={"layer": "KITCHEN"})
            msp.add_line((kitchen_x0, mid_y), (centerline_end or cursor_x, mid_y),
                         dxfattribs={"layer": "KITCHEN", "linetype": "DASHED"})

            # Tie the chain out to the room's end so the run reconciles with
            # the overall width: a trailing gap between the last fixture and
            # the wall (or bathroom partition) gets its own dimension instead
            # of being left as an unexplained short-fall under the overall.
            if kitchen_x1 - cursor_x > 50:
                boundary_xs.append(kitchen_x1)

            # Segment chain just below the counter, inside the room.
            _dim_chain(msp, band_y0 - 250, band_y0, boundary_xs, layer="DIMS")

    # --- Front (bottom) glazing-frame wall with sliding door(s) ---
    # The frame band occupies the wall cavity zone (oy..iy0), INSIDE the
    # container envelope, so the drawn exterior depth always matches the
    # dimensioned width - nothing protrudes below the outline.
    door_ranges = [(d0, d1) for d0, d1, _ in doors]
    if doors:
        joints = _front_frame_joints(ox, length, thickness, door_ranges, partition_faces)
        msp.add_line((ix0, iy0), (ix1, iy0), dxfattribs={"layer": "GLAZING"})
        for x in joints:
            if ix0 < x < ix1:
                msp.add_line((x, oy), (x, iy0), dxfattribs={"layer": "GLAZING"})
        # Each sliding door: heavier double track lines across its opening.
        for d_x0, d_x1, _ in doors:
            msp.add_line((d_x0, oy + thickness * 0.3), (d_x1, oy + thickness * 0.3),
                         dxfattribs={"layer": "DOOR"})
            msp.add_line((d_x0, oy + thickness * 0.7), (d_x1, oy + thickness * 0.7),
                         dxfattribs={"layer": "DOOR"})

    # --- Fold-out deck below the front wall ---
    deck_bottom_y = oy
    if deck and doors:
        deck_depth = deck.get("depth_mm", 2262)
        deck_y1 = oy
        deck_y0 = deck_y1 - deck_depth
        deck_bottom_y = deck_y0
        rail = 60

        _rect(msp, ix0, deck_y0, ix1, deck_y1, "DECK")
        # Side + bottom rails as inner parallel lines.
        msp.add_line((ix0 + rail, deck_y0), (ix0 + rail, deck_y1), dxfattribs={"layer": "DECK"})
        msp.add_line((ix1 - rail, deck_y0), (ix1 - rail, deck_y1), dxfattribs={"layer": "DECK"})
        msp.add_line((ix0, deck_y0 + rail), (ix1, deck_y0 + rail), dxfattribs={"layer": "DECK"})

        # Dashed projection lines from every glazing-frame joint down
        # through the deck - the reference convention tying the folded
        # glass wall to the deck it opens onto.
        for x in _front_frame_joints(ox, length, thickness, door_ranges, partition_faces):
            if ix0 < x < ix1:
                msp.add_line((x, oy), (x, deck_y0),
                             dxfattribs={"layer": "DECK", "linetype": "DASHED"})

        # Deck depth on the right side (offset -350 puts the dimension line
        # to the right of the deck edge; see _dim_chain_vertical on signs).
        _dim(msp, (ix1, deck_y0), (ix1, deck_y1), -350, "DIMS")

    # --- Door callouts, centered in the deck / just inside the room ---
    for d_x0, d_x1, d in doors:
        d_h = d.get("height_mm", 2250)
        callout = f"D:{round(d_x1 - d_x0)}*{round(d_h)} mm"
        # With a deck the callout sits inside it (reference convention).
        # Without one it goes inside the room above the door - below the
        # wall it would land exactly on the joint-chain text row.
        callout_y = deck_bottom_y + 170 if deck else iy0 + 200
        _mtext_simple(msp, callout, (d_x0 + d_x1) / 2.0, callout_y, 130, "TEXT")

    # --- Bottom breakdown chain, anchored exterior-to-exterior ---
    # Sums to the container's overall length by construction, so it can
    # never contradict the top overall dimension (no separate bottom
    # overall row is needed - the top one already gives the total).
    bottom_dim_y = deck_bottom_y
    if doors or partition_faces:
        row_y = bottom_dim_y - 350
        joints = _front_frame_joints(ox, length, thickness, door_ranges, partition_faces)
        _dim_chain(msp, row_y, bottom_dim_y, joints, layer="DIMS")
        bottom_dim_y = row_y - 380

    # --- Outermost dimensions: overall length above, overall width left ---
    # Back-wall window width dims + "W:" callouts stack above the top wall
    # (to +400); lift the overall-length dimension clear of them.
    top_offset = 700 if windows else 330
    _dim(msp, (ox, oy + width), (ox + length, oy + width), top_offset, "DIMS")
    _dim_chain_vertical(msp, ox - 330, ox, [oy, oy + width], layer="DIMS")

    title = plan.get("title")
    _title_below(msp, title, ox + length / 2.0, bottom_dim_y - 450)


# ---------------------------------------------------------------------------
# Elevation shell helpers (shared by front/side/back)
# ---------------------------------------------------------------------------

CASTING_MM = 180   # ISO corner casting size as seen in elevation
TEETH_H_MM = 45    # corrugated roof-edge tab height above the top line
BASE_H_MM = 65     # base/skid band height


def _diag_hatch(msp, x0, y0, x1, y1, spacing, layer, linetype=None):
    """Parallel 45-degree lines properly clipped to a rectangle - used for
    glass streaks (sparse) and the ground/base band (dense)."""
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        return
    attribs = {"layer": layer}
    if linetype:
        attribs["linetype"] = linetype
    c = (y0 - x1) + spacing  # family of lines y = x + c
    while c < y1 - x0:
        ex0 = max(x0, y0 - c)
        ex1 = min(x1, y1 - c)
        if ex0 < ex1:
            msp.add_line((ex0, ex0 + c), (ex1, ex1 + c), dict(attribs))
        c += spacing


def _corner_casting(msp, corner_x, corner_y, dx, dy, layer="OUTLINE"):
    """ISO corner casting: a square with an elliptical hole, drawn inward
    from a shell corner. dx/dy are +1/-1 pointing into the shell."""
    x1 = corner_x + dx * CASTING_MM
    y1 = corner_y + dy * CASTING_MM
    _rect(msp, min(corner_x, x1), min(corner_y, y1), max(corner_x, x1), max(corner_y, y1), layer)
    msp.add_circle(
        center=((corner_x + x1) / 2.0, (corner_y + y1) / 2.0),
        radius=CASTING_MM * 0.22,
        dxfattribs={"layer": layer},
    )


def _elevation_shell(msp, ox, oy, w, h, corrugated=False):
    """Container elevation shell: outline, four corner castings, the
    corrugated roof-edge tabs along the top, and optional full-face
    corrugation (vertical ribbing)."""
    _rect(msp, ox, oy, ox + w, oy + h, "OUTLINE")

    _corner_casting(msp, ox, oy, 1, 1)
    _corner_casting(msp, ox + w, oy, -1, 1)
    _corner_casting(msp, ox, oy + h, 1, -1)
    _corner_casting(msp, ox + w, oy + h, -1, -1)

    # Roof-edge tabs: small rectangles standing proud of the top line.
    tab_w, tab_gap = 140, 120
    x = ox + CASTING_MM + tab_gap
    while x + tab_w < ox + w - CASTING_MM:
        _rect(msp, x, oy + h, x + tab_w, oy + h + TEETH_H_MM, "CORRUGATION")
        x += tab_w + tab_gap

    if corrugated:
        spacing = 150
        x = ox + spacing
        while x < ox + w - spacing / 2.0:
            msp.add_line((x, oy + CASTING_MM), (x, oy + h - CASTING_MM),
                         dxfattribs={"layer": "CORRUGATION"})
            x += spacing


def _leader_callout(msp, text, tx, ty, target, layer="CALLOUTS", height=100):
    """Callout text with a straight leader line to the feature it names,
    matching the reference convention (no arrowhead, thin line)."""
    _mtext_simple(msp, text, tx, ty, height, layer)
    # Leader starts just under the text and runs to the target.
    start_y = ty - height * 0.8 if target[1] < ty else ty + height * 0.8
    msp.add_line((tx, start_y), target, dxfattribs={"layer": layer})


# ---------------------------------------------------------------------------
# Front elevation
# ---------------------------------------------------------------------------

def draw_front_elevation(msp, spec, origin):
    """Front (glass-wall) elevation following the reference sheets: shell
    with castings and roof tabs, hatched base band, double-framed glazing
    panels with diagonal glass streaks, slider sub-panels for the door,
    dashed cable braces, leader callouts, stacked bottom dimension rows,
    and glass-height + overall vertical dimensions."""
    ox, oy = origin
    container = spec.get("container", {})
    length = container.get("length_mm", 6058)

    front = spec.get("front_elevation", {})
    height = front.get("height_mm", container.get("height_mm", 2896))

    _elevation_shell(msp, ox, oy, length, height)

    panels = front.get("glazing_panels")
    if panels:
        total_w = sum(p.get("width_mm", 0) for p in panels)
        fx0 = ox + max(CASTING_MM, (length - total_w) / 2.0)
        gz_y0 = oy + BASE_H_MM
        gz_y1 = oy + height - 100  # top frame margin under the roof line

        # Hatched base band under the glass wall.
        _rect(msp, fx0, oy, fx0 + total_w, gz_y0, "OUTLINE")
        _diag_hatch(msp, fx0, oy, fx0 + total_w, gz_y0, 90, "CORRUGATION")

        boundary_xs = [fx0]
        cursor_x = fx0
        glass_panels = []
        for panel in panels:
            pw = panel.get("width_mm", 0)
            x0, x1 = cursor_x, cursor_x + pw
            ptype = panel.get("type", "")

            _rect(msp, x0, gz_y0, x1, gz_y1, "GLAZING")
            if ptype == "sliding_glass":
                # Slider: two sub-panels, each with an inner frame + streaks.
                mid = (x0 + x1) / 2.0
                for sx0, sx1 in ((x0, mid), (mid, x1)):
                    _rect(msp, sx0 + 40, gz_y0 + 40, sx1 - 40, gz_y1 - 40, "GLAZING")
                    _diag_hatch(msp, sx0 + 100, gz_y0 + 100, sx1 - 100, gz_y1 - 100, 780, "GLAZING")
                glass_panels.append((x0, x1))
            elif ptype == "fixed_glass":
                _rect(msp, x0 + 40, gz_y0 + 40, x1 - 40, gz_y1 - 40, "GLAZING")
                _diag_hatch(msp, x0 + 100, gz_y0 + 100, x1 - 100, gz_y1 - 100, 720, "GLAZING")
                glass_panels.append((x0, x1))
            # "frame" panels stay as the bare structural rectangle.

            cursor_x = x1
            boundary_xs.append(cursor_x)

        # Dashed cable braces across the end glass bays + leader callouts.
        if front.get("cable_bracing") and glass_panels:
            for bx0, bx1 in (glass_panels[0], glass_panels[-1]):
                msp.add_line((bx0, gz_y0), (bx1, gz_y1),
                             dxfattribs={"layer": "CALLOUTS", "linetype": "DASHED"})
            lx0, _ = glass_panels[0]
            _leader_callout(msp, "diagonal cable wire",
                            ox - 500, oy + height * 0.62,
                            (lx0 + 260, gz_y0 + (gz_y1 - gz_y0) * 0.55))

        # Frame callouts with leaders: first to the corner post, the rest
        # distributed across the mullions.
        callouts = front.get("frame_callouts") or []
        mullions = boundary_xs[1:-1] or [fx0]
        for i, callout in enumerate(callouts):
            if i == 0:
                target = (fx0, gz_y1 - 60)
                tx = fx0 - 300
            else:
                m = mullions[min(i - 1, len(mullions) - 1)]
                target = (m, gz_y1 - 40)
                tx = m
            _leader_callout(msp, callout, tx, oy + height + 320 + (i % 2) * 220, target)

        # Stacked bottom rows: panel chain, then overall frame span.
        _dim_chain(msp, oy - 350, oy, boundary_xs, layer="DIMS")
        _dim(msp, (fx0, oy), (fx0 + total_w, oy), -700, "DIMS")

        # Vertical dims on the right: glass height, then overall height.
        _dim(msp, (ox + length, gz_y0), (ox + length, gz_y1), -330, "DIMS")
        _dim(msp, (ox + length, oy), (ox + length, oy + height), -660, "DIMS")

    title = front.get("title")
    _title_below(msp, title, ox + length / 2.0, oy - 1150)


# ---------------------------------------------------------------------------
# Side elevation
# ---------------------------------------------------------------------------

def draw_side_elevation(msp, spec, origin):
    """Side (container end) elevation: corrugated shell with castings and
    roof tabs, the fold-out floor slab deployed to the left with its dashed
    swing arc, and width/height dimensions - following the reference sheets."""
    ox, oy = origin
    container = spec.get("container", {})
    width = container.get("width_mm", 2438)
    height = container.get("height_mm", 2896)

    _elevation_shell(msp, ox, oy, width, height, corrugated=True)

    side = spec.get("side_elevation", {})
    platform = side.get("fold_out_platform")
    if platform:
        pw = platform.get("width_mm", 0)
        radius = platform.get("swing_radius_mm", pw)

        # Deployed floor slab extending from the container base to the left
        # (the side the wall folds open onto), with a hatched edge.
        slab_x0, slab_x1 = ox - pw, ox
        _rect(msp, slab_x0, oy, slab_x1, oy + BASE_H_MM, "CALLOUTS")
        _diag_hatch(msp, slab_x0, oy, slab_x1, oy + BASE_H_MM, 90, "CORRUGATION")

        # Dashed arc sweeping between deployed (horizontal) and stowed
        # (vertical, against the container wall) positions.
        msp.add_arc(
            center=(ox, oy + BASE_H_MM),
            radius=radius,
            start_angle=90,
            end_angle=180,
            dxfattribs={"layer": "CALLOUTS", "linetype": "DASHED"},
        )

        # Slab depth dimension below, then the label with a leader.
        _dim(msp, (slab_x0, oy), (slab_x1, oy), -350, "DIMS")
        label = side.get("floor_extension_label")
        if label:
            _leader_callout(msp, label, slab_x0 + pw * 0.35, oy - 800,
                            (slab_x0 + pw * 0.5, oy - 20))

    # Overall width below, overall height on the right.
    _dim(msp, (ox, oy), (ox + width, oy), -700, "DIMS")
    _dim(msp, (ox + width, oy), (ox + width, oy + height), -330, "DIMS")

    title = side.get("title")
    _title_below(msp, title, ox + width / 2.0, oy - 1150)


# ---------------------------------------------------------------------------
# Back elevation
# ---------------------------------------------------------------------------

def draw_back_elevation(msp, spec, origin):
    """Back elevation: fully corrugated shell (the reference's long wall is
    the corrugated one - drawn at container length, not width), with the
    vent window cut out of the ribbing and dimensioned the way the
    references do: width above, sill height below, position chain and
    overall length at the bottom, overall height on the left."""
    ox, oy = origin
    container = spec.get("container", {})
    length = container.get("length_mm", 6058)
    height = container.get("height_mm", 2896)

    back = spec.get("back_elevation", {})
    vent = back.get("vent_window")

    vent_bounds = None
    if vent:
        vw = vent.get("width_mm", 0)
        vh = vent.get("height_mm", 0)
        x0 = ox + vent.get("position_from_left_mm", 0)
        x1 = x0 + vw
        center_h = vent.get("center_height_mm", height / 2.0)
        y0 = oy + center_h - vh / 2.0
        y1 = oy + center_h + vh / 2.0
        vent_bounds = (x0, y0, x1, y1)

    _elevation_shell(msp, ox, oy, length, height, corrugated=False)

    # Corrugation ribbing, interrupted across the vent window.
    spacing = 150
    x = ox + spacing
    while x < ox + length - spacing / 2.0:
        if vent_bounds and vent_bounds[0] <= x <= vent_bounds[2]:
            msp.add_line((x, oy + CASTING_MM), (x, vent_bounds[1]),
                         dxfattribs={"layer": "CORRUGATION"})
            msp.add_line((x, vent_bounds[3]), (x, oy + height - CASTING_MM),
                         dxfattribs={"layer": "CORRUGATION"})
        else:
            msp.add_line((x, oy + CASTING_MM), (x, oy + height - CASTING_MM),
                         dxfattribs={"layer": "CORRUGATION"})
        x += spacing

    if vent_bounds:
        x0, y0, x1, y1 = vent_bounds
        # Window with an inner frame.
        _rect(msp, x0, y0, x1, y1, "OUTLINE")
        _rect(msp, x0 + 40, y0 + 40, x1 - 40, y1 - 40, "GLAZING")
        # Width dim above the window, sill height below it.
        _dim(msp, (x0, y1), (x1, y1), 200, "DIMS")
        _dim(msp, ((x0 + x1) / 2.0, oy), ((x0 + x1) / 2.0, y0), (x0 + x1) / 2.0 - (x1 + 300), "DIMS")
        # Bottom rows: position chain, then overall length.
        _dim_chain(msp, oy - 350, oy, [ox, x0, x1, ox + length], layer="DIMS")
        _dim(msp, (ox, oy), (ox + length, oy), -700, "DIMS")
    else:
        _dim(msp, (ox, oy), (ox + length, oy), -350, "DIMS")

    # Overall height on the left.
    _dim_chain_vertical(msp, ox - 330, ox, [oy, oy + height], layer="DIMS")

    title = back.get("title")
    _title_below(msp, title, ox + length / 2.0, oy - 1150)


# ---------------------------------------------------------------------------
# Top-level builder
# ---------------------------------------------------------------------------

def _add_layers(doc):
    # Monochrome, weight-differentiated linework matching the reference shop
    # drawings: heavy container walls, medium fixtures, thin dimensions and
    # texture. Color 7 renders black on white paper/preview (and follows the
    # background convention in CAD viewers). Lineweight is in 1/100 mm.
    layer_defs = [
        ("OUTLINE", 50),
        ("KITCHEN", 25),
        ("GLAZING", 18),
        ("DIMS", 13),
        ("TEXT", 25),
        ("CORRUGATION", 13),
        ("CALLOUTS", 13),
        ("INSULATION", 13),
        ("DOOR", 25),
        ("DECK", 35),
    ]
    for name, lineweight in layer_defs:
        if name not in doc.layers:
            layer = doc.layers.add(name, color=7)
            layer.dxf.lineweight = lineweight


def _derive_elevation_sections(spec: dict) -> dict:
    """Fill in missing elevation sections from what the plan already knows,
    so "Generate elevations too" shows the actual design (glass front wall,
    fold-out floor, back-wall windows) instead of empty container shells.
    Explicit elevation sections from the spec always win. Returns a copy;
    the client's spec is never mutated."""
    plan = spec.get("plan", {})
    out = dict(spec)
    container = spec.get("container", {})
    length = container.get("length_mm", 6058)
    t = container.get("wall_thickness_mm", DEFAULT_WALL_THICKNESS_MM)

    # Gather all doors (single + array) the same way the plan view does.
    door_specs = []
    if plan.get("sliding_door"):
        door_specs.append(plan["sliding_door"])
    door_specs.extend(plan.get("sliding_doors") or [])
    door_specs = sorted(door_specs, key=lambda d: d.get("position_from_left_mm", 0))

    if "front_elevation" not in out and door_specs:
        # Walk across the interior width, emitting a fixed-glass panel for
        # each stretch between doors and a sliding-glass panel per door - so
        # a two-door request renders two sliding bays, not one.
        interior_x0, interior_x1 = t, length - t
        panels = []
        cursor = interior_x0
        for d in door_specs:
            d_x0 = d.get("position_from_left_mm", 0)
            d_x1 = d_x0 + d.get("width_mm", 0)
            if d_x0 - cursor > 1:
                panels.append({"width_mm": d_x0 - cursor, "type": "fixed_glass"})
            panels.append({"width_mm": max(0.0, d_x1 - d_x0), "type": "sliding_glass"})
            cursor = d_x1
        if interior_x1 - cursor > 1:
            panels.append({"width_mm": interior_x1 - cursor, "type": "fixed_glass"})
        out["front_elevation"] = {"title": "Front Elevation", "glazing_panels": panels}

    if "side_elevation" not in out and plan.get("deck"):
        depth = plan["deck"].get("depth_mm", 2262)
        out["side_elevation"] = {
            "title": "Side Elevation",
            "fold_out_platform": {"width_mm": depth, "swing_radius_mm": depth + 190},
            "floor_extension_label": "fold-out floor",
        }

    windows = plan.get("windows") or []
    if "back_elevation" not in out and windows:
        w = windows[0]
        out["back_elevation"] = {
            "title": "Back Elevation",
            "vent_window": {
                "width_mm": w.get("width_mm", 600),
                "height_mm": w.get("height_mm", 600),
                "position_from_left_mm": w.get("position_from_left_mm", 0),
                # Sill height isn't modeled in plan; a kitchen-window head
                # height around 1500 center is the references' convention.
                "center_height_mm": 1500,
            },
        }

    return out


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
    if requested - {"plan"}:
        spec = _derive_elevation_sections(spec)

    container = spec.get("container", {})
    length = container.get("length_mm", 6058)
    height = container.get("height_mm", 2896)

    # BELOW_MARGIN_MM is a conservative allowance for the stacked dimension
    # rows and underlined title a view draws below its own base rectangle,
    # so the next row's origin can be computed without geometry colliding.
    # The plan view additionally hangs its fold-out deck below the container.
    BELOW_MARGIN_MM = 1600

    cursor_y = 0

    if "plan" in requested:
        draw_plan_view(msp, spec, (0, cursor_y))
        plan = spec.get("plan", {})
        has_door = bool(plan.get("sliding_door") or plan.get("sliding_doors"))
        deck_drop = 0
        if plan.get("deck") and has_door:
            deck_drop = plan["deck"].get("depth_mm", 2262) + 60
        cursor_y = cursor_y - deck_drop - BELOW_MARGIN_MM - ROW_GAP_MM

    if "front" in requested or "side" in requested:
        front = spec.get("front_elevation", {})
        front_height = front.get("height_mm", height)
        front_origin_y = cursor_y - front_height

        if "front" in requested:
            draw_front_elevation(msp, spec, (0, front_origin_y))
        if "side" in requested:
            # Leave room for the fold-out floor slab the side view deploys
            # to its left, plus the front view's right-hand vertical dims.
            side = spec.get("side_elevation", {})
            platform = side.get("fold_out_platform") or {}
            side_x = length + COL_GAP_MM + platform.get("width_mm", 0) + 500
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
