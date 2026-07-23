"""Shared ezdxf Drawing -> bytes serialization, used by both cad_engine and
container_engine. No disk writes anywhere in this module."""
import io

from ezdxf.addons.drawing import RenderContext, Frontend
from ezdxf.addons.drawing.config import BackgroundPolicy, Configuration
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


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
