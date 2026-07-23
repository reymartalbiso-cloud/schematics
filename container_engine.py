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

    bottom_gap = (ox, ox + length) if glass_front else None
    _rect_open_sides(msp, ox, oy, ox + length, oy + width, "OUTLINE", bottom_gap=bottom_gap)
    _rect_open_sides(msp, ix0, iy0, ix1, iy1, "OUTLINE", bottom_gap=bottom_gap)
    if glass_front:
        # Close the exposed wall-cavity ends at the front corners.
        msp.add_line((ox, oy), (ix0, oy), dxfattribs={"layer": "OUTLINE"})
        msp.add_line((ix1, oy), (ox + length, oy), dxfattribs={"layer": "OUTLINE"})

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


def _front_frame_joints(frame_x0, frame_x1, door_x0, door_x1, post=100, gap=50):
    """X positions of the glazing-frame joints across the front wall:
    end post | side panel | gap | sliding door | gap | side panel | end post.
    Matches the reference breakdown (e.g. 100/1654/50/2000/50/1654/100)."""
    joints = [frame_x0, frame_x0 + post, door_x0 - gap, door_x0, door_x1, door_x1 + gap, frame_x1 - post, frame_x1]
    # Collapse out-of-order joints (door hard against one end, tiny panels).
    return sorted(set(x for x in joints if frame_x0 <= x <= frame_x1))


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
    sliding_door = plan.get("sliding_door")
    windows = plan.get("windows") or []
    deck = plan.get("deck")

    door_x0 = door_x1 = None
    if sliding_door:
        door_width = sliding_door.get("width_mm", 0)
        pos_from_left = sliding_door.get("position_from_left_mm", 0)
        door_x0 = ox + pos_from_left
        door_x1 = door_x0 + door_width

    back_gaps = []
    for win in windows:
        w_x0 = ox + win.get("position_from_left_mm", 0)
        back_gaps.append((w_x0, w_x0 + win.get("width_mm", 0)))

    ix0, iy0, ix1, iy1 = _draw_container_walls(
        msp, ox, oy, length, width, thickness,
        glass_front=bool(sliding_door), back_gaps=back_gaps,
    )

    # --- Back (top) wall windows: jambs, slider panes, dim, W: callout ---
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
        _dim(msp, (w_x0, iy1), (w_x1, iy1), -160, "DIMS")
        callout = f"W:{round(win.get('width_mm', 0))}*{round(win.get('height_mm', 0))}mm"
        _mtext_simple(msp, callout, (w_x0 + w_x1) / 2.0, oy + width + 170, 110, "TEXT")

    # --- Kitchen run along the back (top) wall ---
    if kitchen_run:
        depth = kitchen_run.get("depth_mm", 700)
        segments = kitchen_run.get("segments", [])
        if segments:
            band_y1 = iy1
            band_y0 = band_y1 - depth
            mid_y = (band_y0 + band_y1) / 2.0
            cursor_x = ix0
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
            msp.add_line((ix0, band_y0), (cursor_x, band_y0), dxfattribs={"layer": "KITCHEN"})
            msp.add_line((ix0, mid_y), (centerline_end or cursor_x, mid_y),
                         dxfattribs={"layer": "KITCHEN", "linetype": "DASHED"})

            # Segment chain just below the counter, inside the room.
            _dim_chain(msp, band_y0 - 250, band_y0, boundary_xs, layer="DIMS")

    # --- Front (bottom) glazing-frame wall with sliding door ---
    if sliding_door:
        frame_y1 = oy
        frame_y0 = oy - 60
        joints = _front_frame_joints(ix0, ix1, door_x0, door_x1)
        msp.add_line((ix0, frame_y1), (ix1, frame_y1), dxfattribs={"layer": "GLAZING"})
        msp.add_line((ix0, frame_y0), (ix1, frame_y0), dxfattribs={"layer": "GLAZING"})
        for x in joints:
            msp.add_line((x, frame_y0), (x, frame_y1), dxfattribs={"layer": "GLAZING"})
        # Sliding door: heavier double track lines across its opening.
        msp.add_line((door_x0, frame_y0 + 15), (door_x1, frame_y0 + 15), dxfattribs={"layer": "DOOR"})
        msp.add_line((door_x0, frame_y1 - 15), (door_x1, frame_y1 - 15), dxfattribs={"layer": "DOOR"})

    # --- Fold-out deck below the front wall ---
    deck_bottom_y = oy
    if deck and sliding_door:
        deck_depth = deck.get("depth_mm", 2262)
        deck_y1 = oy - 60
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
        for x in _front_frame_joints(ix0, ix1, door_x0, door_x1)[1:-1]:
            msp.add_line((x, oy - 60), (x, deck_y0),
                         dxfattribs={"layer": "DECK", "linetype": "DASHED"})

        # Deck depth on the right side (offset -350 puts the dimension line
        # to the right of the deck edge; see _dim_chain_vertical on signs).
        _dim(msp, (ix1, deck_y0), (ix1, deck_y1), -350, "DIMS")

    # --- Door callout, centered in the deck / below the door ---
    if sliding_door:
        d_h = sliding_door.get("height_mm", 2250)
        callout = f"D:{round(door_x1 - door_x0)}*{round(d_h)} mm"
        callout_y = deck_bottom_y + 170 if deck else oy - 250
        _mtext_simple(msp, callout, (door_x0 + door_x1) / 2.0, callout_y, 130, "TEXT")

    # --- Stacked bottom dimension rows ---
    bottom_dim_y = deck_bottom_y
    if sliding_door:
        row_y = bottom_dim_y - 350
        joints = _front_frame_joints(ix0, ix1, door_x0, door_x1)
        _dim_chain(msp, row_y, bottom_dim_y, joints, layer="DIMS")
        _dim(msp, (ix0, bottom_dim_y), (ix1, bottom_dim_y), row_y - 350 - bottom_dim_y, "DIMS")
        bottom_dim_y = row_y - 350

    # --- Outermost dimensions: overall length above, overall width left ---
    _dim(msp, (ox, oy + width), (ox + length, oy + width), 330, "DIMS")
    _dim_chain_vertical(msp, ox - 330, ox, [oy, oy + width], layer="DIMS")

    title = plan.get("title")
    _title_below(msp, title, ox + length / 2.0, bottom_dim_y - 450)


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
