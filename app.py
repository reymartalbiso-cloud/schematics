"""Stateless Flask backend. The browser holds current_spec; every route rebuilds
in-memory from client-supplied JSON - no server-side session store, no disk writes."""
import base64
import io
import os
import secrets
import shutil
import subprocess
import tempfile

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.exceptions import HTTPException

# Must run before auth/claude_client below, since both read env vars at import time.
load_dotenv()

import auth
import cad_engine
import claude_client
import container_engine
import designs
import memory

# Explicit opt-in only - defaults to the safer (production) setting so a
# missing env var never silently downgrades cookie security.
_DEBUG = os.environ.get("FLASK_DEBUG") == "1"

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=not _DEBUG,
)

ENGINES = {"floorplan": cad_engine, "container": container_engine}
_PUBLIC_PATHS = {"/login", "/favicon.ico"}


@app.after_request
def _security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@app.errorhandler(Exception)
def _handle_unexpected_error(exc):
    if isinstance(exc, HTTPException):
        return exc
    if request.path.startswith("/api/"):
        app.logger.exception("Unhandled error in %s", request.path)
        return jsonify({"error": "Internal server error"}), 500
    raise exc


def _safe_next_path(candidate: str | None) -> str | None:
    # Only allow same-site relative paths - an absolute URL or protocol-relative
    # "//evil.com" here would make /login an open redirect.
    if candidate and candidate.startswith("/") and not candidate.startswith("//"):
        return candidate
    return None


def _generate_spec(mode, text, current_spec, context_block):
    # Looked up via the module (not bound at import time) so tests can
    # monkeypatch claude_client.generate_*_spec directly.
    return getattr(claude_client, f"generate_{mode}_spec")(text, current_spec, context_block)


def _build_doc(mode, spec, views=None):
    # Only container mode has a views concept (plan vs. full elevation
    # sheet); floorplan is always a single view.
    engine = ENGINES[mode]
    if mode == "container":
        return engine.build_doc(spec, views=views)
    return engine.build_doc(spec)


def _build_dxf_bytes(mode, spec, views=None):
    """Build DXF bytes from a client-supplied spec, or None + an error message
    if the spec doesn't describe valid geometry."""
    try:
        return ENGINES[mode].doc_to_dxf_bytes(_build_doc(mode, spec, views)), None
    except Exception as exc:
        return None, str(exc)


@app.before_request
def _enforce_auth():
    if request.path.startswith("/static/") or request.path in _PUBLIC_PATHS:
        return None
    if auth.is_authenticated():
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "authentication required"}), 401
    return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not auth.auth_enabled():
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if auth.check_password(request.form.get("password", "")):
            session["authenticated"] = True
            next_path = _safe_next_path(request.args.get("next"))
            return redirect(next_path or url_for("index"))
        error = "Incorrect password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.pop("authenticated", None)
    return redirect(url_for("login"))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/prompt", methods=["POST"])
def api_prompt():
    data = request.get_json(force=True) or {}
    mode = data.get("mode")
    text = (data.get("text") or "").strip()
    current_spec = data.get("current_spec")
    views = data.get("views")

    if mode not in ENGINES:
        return jsonify({"error": "invalid mode"}), 400
    if not text:
        return jsonify({"error": "text is required"}), 400

    mem = memory.load_memory()
    context_block = memory.build_context_block(mode, mem)
    try:
        spec = _generate_spec(mode, text, current_spec, context_block)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    doc = _build_doc(mode, spec, views)
    preview_uri = _to_data_uri(ENGINES[mode].doc_to_preview_bytes(doc))

    memory.log_design(mode, memory.summarize_spec(mode, spec), mem)

    return jsonify(
        {
            "spec": spec,
            "preview_data_uri": preview_uri,
            "librecad_installed": _librecad_path() is not None,
        }
    )


@app.route("/api/render", methods=["POST"])
def api_render():
    """Re-render a preview from an already-generated spec without calling
    Claude again - used for the container-mode "generate elevations too"
    action, which only changes which views are drawn, not the spec itself."""
    data = request.get_json(force=True) or {}
    mode, spec = data.get("mode"), data.get("spec")
    views = data.get("views")
    if mode not in ENGINES or spec is None:
        return jsonify({"error": "mode and spec are required"}), 400

    try:
        doc = _build_doc(mode, spec, views)
        preview_uri = _to_data_uri(ENGINES[mode].doc_to_preview_bytes(doc))
    except Exception as exc:
        return jsonify({"error": f"Invalid spec: {exc}"}), 400

    return jsonify({"preview_data_uri": preview_uri})


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(force=True) or {}
    mode, spec = data.get("mode"), data.get("spec")
    views = data.get("views")
    if mode not in ENGINES or spec is None:
        return jsonify({"error": "mode and spec are required"}), 400

    dxf_bytes, error = _build_dxf_bytes(mode, spec, views)
    if error:
        return jsonify({"error": f"Invalid spec: {error}"}), 400

    filename = "floorplan.dxf" if mode == "floorplan" else "container_home.dxf"
    return send_file(
        io.BytesIO(dxf_bytes),
        mimetype="application/dxf",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/api/open_librecad", methods=["POST"])
def api_open_librecad():
    data = request.get_json(force=True) or {}
    mode, spec = data.get("mode"), data.get("spec")
    views = data.get("views")
    if mode not in ENGINES or spec is None:
        return jsonify({"error": "mode and spec are required"}), 400

    librecad_path = _librecad_path()
    if not librecad_path:
        return jsonify({"error": "LibreCAD is not installed on this machine"}), 404

    dxf_bytes, error = _build_dxf_bytes(mode, spec, views)
    if error:
        return jsonify({"error": f"Invalid spec: {error}"}), 400

    fd, path = tempfile.mkstemp(suffix=".dxf")
    with os.fdopen(fd, "wb") as f:
        f.write(dxf_bytes)
    subprocess.Popen([librecad_path, path])
    return jsonify({"opened": True})


@app.route("/api/memory", methods=["GET"])
def api_memory_get():
    return jsonify(memory.load_memory())


@app.route("/api/memory/preference", methods=["POST"])
def api_memory_add_preference():
    text = ((request.get_json(force=True) or {}).get("text") or "").strip()
    if not text:
        return jsonify({"error": "text is required"}), 400
    return jsonify(memory.add_preference(text))


@app.route("/api/memory/preference/<int:index>", methods=["DELETE"])
def api_memory_remove_preference(index):
    return jsonify(memory.remove_preference(index))


@app.route("/api/memory/clear", methods=["POST"])
def api_memory_clear():
    return jsonify(memory.clear_memory())


@app.route("/api/designs", methods=["GET"])
def api_designs_list():
    return jsonify(designs.list_designs())


@app.route("/api/designs", methods=["POST"])
def api_designs_save():
    data = request.get_json(force=True) or {}
    mode = data.get("mode")
    title = (data.get("title") or "").strip()
    spec = data.get("spec")
    if mode not in ENGINES or not title or spec is None:
        return jsonify({"error": "mode, title, and spec are required"}), 400
    return jsonify(designs.save_design(mode, title, spec, data.get("id")))


@app.route("/api/designs/<design_id>", methods=["GET"])
def api_designs_get(design_id):
    record = designs.get_design(design_id)
    if record is None or record["mode"] not in ENGINES:
        return jsonify({"error": "not found"}), 404
    engine = ENGINES[record["mode"]]
    record["preview_data_uri"] = _to_data_uri(engine.doc_to_preview_bytes(engine.build_doc(record["spec"])))
    return jsonify(record)


@app.route("/api/designs/<design_id>", methods=["DELETE"])
def api_designs_delete(design_id):
    if not designs.delete_design(design_id):
        return jsonify({"error": "not found"}), 404
    return jsonify({"deleted": True})


def _to_data_uri(png_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


def _librecad_path():
    return shutil.which("librecad")


if __name__ == "__main__":
    app.run(debug=_DEBUG, port=5000)
