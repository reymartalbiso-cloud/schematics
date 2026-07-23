"""Shared ezdxf Drawing -> bytes serialization, used by both cad_engine and
container_engine. No disk writes anywhere in this module."""
import io
import math
import os

# Matplotlib builds/caches its font list on first use and defaults to writing
# that cache under the user's home directory. On Vercel's serverless
# filesystem, everything outside /tmp is read-only, so that write silently
# fails and matplotlib falls back to rendering with no text glyphs at all -
# geometry still draws fine, but every label/dimension/title vanishes. This
# must be set before matplotlib is imported anywhere in the process.
os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

from ezdxf.addons.drawing import RenderContext, Frontend
from ezdxf.addons.drawing.config import BackgroundPolicy, Configuration
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
from ezdxf.fonts import fonts as ezdxf_fonts

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Force the font bundled inside matplotlib itself rather than relying on
# system-font discovery, which differs between local machines and the
# serverless environment.
plt.rcParams["font.family"] = "DejaVu Sans"

# ezdxf has its OWN font discovery, entirely separate from matplotlib's: it
# scans platform font directories (/usr/share/fonts etc. on Linux) to build
# a font cache, and the drawing addon renders text as vector paths using
# those fonts. On serverless Linux (Vercel) there are no system fonts at
# all, so the cache comes up empty and ezdxf falls back to a placeholder
# font that renders every glyph as an empty "tofu" rectangle - geometry
# fine, all text as boxes. Matplotlib ships DejaVu TTFs inside its own pip
# package, which is guaranteed present here, so register that directory
# with ezdxf's font manager. Runs once at import; harmless when system
# fonts also exist.
_MPL_FONT_DIR = os.path.join(os.path.dirname(matplotlib.__file__), "mpl-data", "fonts", "ttf")


def _register_bundled_fonts() -> None:
    try:
        fm = ezdxf_fonts.font_manager
        # clear() first: besides dropping any (possibly empty) system scan,
        # it resets the memoized fallback-font name, which - once computed
        # against an empty cache - stays poisoned even after fonts appear.
        fm.clear()
        # The match cache holds negative ("no font found") results that
        # clear()/build() never invalidate; drop it so queries made before
        # registration can't pin rendering to the tofu fallback.
        if hasattr(fm, "_match_cache"):
            fm._match_cache.clear()
        fm.build([_MPL_FONT_DIR], support_dirs=False)
    except Exception:
        # A failed scan must never take down rendering; worst case is the
        # pre-existing behavior (tofu boxes on fontless systems).
        pass


_register_bundled_fonts()


# ---------------------------------------------------------------------------
# Small vector helpers, shared by cad_engine.py and container_engine.py.
# ---------------------------------------------------------------------------

def sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def add(a, b):
    return (a[0] + b[0], a[1] + b[1])


def scale(v, s):
    return (v[0] * s, v[1] * s)


def length(v):
    return math.hypot(v[0], v[1])


def unit(v):
    d = length(v)
    return (v[0] / d, v[1] / d)


def perp(v):
    # Rotate 90 degrees counter-clockwise.
    return (-v[1], v[0])


# ---------------------------------------------------------------------------
# Manual dimension drawing.
#
# ezdxf's add_linear_dim()/.render() produces a real DIMENSION entity with its
# measurement text pre-rendered into an anonymous block. That's correct,
# standards-compliant DXF - confirmed by direct inspection of generated
# files, down to the per-entity "ACAD/DSTYLE" XDATA override and the base
# DIMSTYLE table record both carrying the right values. But real-world
# testing found LibreCAD never displays that text regardless: not with
# per-entity overrides, not with the base DIMSTYLE table's own defaults
# changed directly. Plain MTEXT and MTEXT embedded in an ordinary block both
# render correctly in the same LibreCAD build, so the gap is specific to how
# LibreCAD (re-)renders DIMENSION entities, not a data or styling problem.
#
# Rather than depend on every DXF reader's own dimension-rendering engine,
# draw dimensions by hand from plain LINE + MTEXT entities - the same
# primitives already proven to render identically everywhere.
# ---------------------------------------------------------------------------

def draw_measurement(
    msp,
    p1,
    p2,
    offset,
    layer,
    text=None,
    text_height=120,
    gap=50,
    overshoot=50,
    tick_size=80,
):
    """Draw one dimension (extension lines, dimension line, end ticks, and
    the measurement text) between p1 and p2.

    offset: signed perpendicular distance from the p1->p2 line to the
        dimension line itself (sign picks which side it's drawn on).
    text: overrides the auto-computed "<length>" label, e.g. for a chain
        that should show a running total instead of a segment length.
    """
    direction = unit(sub(p2, p1))
    perpendicular = perp(direction)
    side = 1.0 if offset >= 0 else -1.0
    side_vec = scale(perpendicular, side)

    dim_p1 = add(p1, scale(perpendicular, offset))
    dim_p2 = add(p2, scale(perpendicular, offset))

    ext1_start = add(p1, scale(side_vec, gap))
    ext1_end = add(dim_p1, scale(side_vec, overshoot))
    msp.add_line(ext1_start, ext1_end, dxfattribs={"layer": layer})

    ext2_start = add(p2, scale(side_vec, gap))
    ext2_end = add(dim_p2, scale(side_vec, overshoot))
    msp.add_line(ext2_start, ext2_end, dxfattribs={"layer": layer})

    msp.add_line(dim_p1, dim_p2, dxfattribs={"layer": layer})

    # 45-degree architectural tick marks at each end of the dimension line.
    tick_dir = unit(add(direction, perpendicular))
    half_tick = scale(tick_dir, tick_size / 2.0)
    msp.add_line(sub(dim_p1, half_tick), add(dim_p1, half_tick), dxfattribs={"layer": layer})
    msp.add_line(sub(dim_p2, half_tick), add(dim_p2, half_tick), dxfattribs={"layer": layer})

    label = text if text is not None else str(round(length(sub(p2, p1))))
    mid = scale(add(dim_p1, dim_p2), 0.5)
    text_pos = add(mid, scale(side_vec, text_height * 0.7))

    # Keep text upright and left-to-right regardless of measurement
    # direction - a wall traversed right-to-left would otherwise render
    # its dimension text upside down.
    angle = math.degrees(math.atan2(direction[1], direction[0]))
    if angle > 90:
        angle -= 180
    elif angle <= -90:
        angle += 180

    mtext = msp.add_mtext(
        label,
        dxfattribs={"layer": layer, "char_height": text_height, "insert": text_pos, "rotation": angle},
    )
    try:
        mtext.dxf.attachment_point = 5  # MIDDLE_CENTER
    except Exception:
        pass


def doc_to_dxf_bytes(doc) -> bytes:
    """Serialize an ezdxf Drawing to DXF file bytes, in-memory."""
    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode("utf-8")


def doc_to_preview_bytes(doc, figsize=(10, 8)) -> bytes:
    """Render the drawing to a PNG image, in-memory, and return the PNG bytes."""
    msp = doc.modelspace()
    fig = plt.figure(figsize=figsize)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor("white")
    ctx = RenderContext(doc)
    backend = MatplotlibBackend(ax)
    config = Configuration(background_policy=BackgroundPolicy.WHITE)
    Frontend(ctx, backend, config=config).draw_layout(msp, finalize=True)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
