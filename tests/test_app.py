import copy
import json

import claude_client
import memory
import auth
from conftest import (
    SAMPLE_CONTAINER_SPEC_NO_KITCHEN,
    SAMPLE_CONTAINER_SPEC_WITH_KITCHEN,
    SAMPLE_FLOORPLAN_SPEC,
)


# ---------------------------------------------------------------------------
# 1. First-generation prompt (floor plan) -> valid spec, DXF, preview
# ---------------------------------------------------------------------------
def test_floorplan_first_generation(client, monkeypatch):
    monkeypatch.setattr(claude_client, "generate_floorplan_spec", lambda *a, **k: copy.deepcopy(SAMPLE_FLOORPLAN_SPEC))

    resp = client.post("/api/prompt", json={"mode": "floorplan", "text": "a living room", "current_spec": None})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["spec"]["walls"][0]["id"] == "w1"
    assert data["preview_data_uri"].startswith("data:image/png;base64,")
    assert isinstance(data["librecad_installed"], bool)

    dl = client.post("/api/download", json={"mode": "floorplan", "spec": data["spec"]})
    assert dl.status_code == 200
    assert dl.mimetype == "application/dxf"
    body = dl.data
    assert len(body) > 0
    assert b"LWPOLYLINE" in body
    # Dimensions are drawn by hand (plain LINE + MTEXT, not a DIMENSION
    # entity) - see dxf_render.draw_measurement for why. Check for the
    # actual measurement text instead: the 6000mm wall's dimension label.
    assert b"6000" in body


# ---------------------------------------------------------------------------
# 2. Edit prompt correctly passes current_spec through without dropping it
# ---------------------------------------------------------------------------
def test_floorplan_edit_preserves_current_spec_context(client, monkeypatch):
    captured = {}

    def fake_generate(text, current_spec=None, context_block="", correction_problems=None):
        captured["text"] = text
        captured["current_spec"] = current_spec
        edited = copy.deepcopy(current_spec)
        edited["rooms"].append({"name": "Bedroom", "area_sqm": 12.0, "label_position": [7000, 2000]})
        return edited

    monkeypatch.setattr(claude_client, "generate_floorplan_spec", fake_generate)

    resp = client.post(
        "/api/prompt",
        json={"mode": "floorplan", "text": "add a bedroom", "current_spec": SAMPLE_FLOORPLAN_SPEC},
    )
    assert resp.status_code == 200
    data = resp.get_json()

    # the edit call received the full prior spec, not a stripped-down version
    assert captured["current_spec"] == SAMPLE_FLOORPLAN_SPEC
    # unrelated elements from the original spec survive in the result
    assert data["spec"]["walls"] == SAMPLE_FLOORPLAN_SPEC["walls"]
    assert len(data["spec"]["rooms"]) == 2


# ---------------------------------------------------------------------------
# 3. Container mode: optional section present, and correctly omitted
# ---------------------------------------------------------------------------
def test_container_mode_with_and_without_kitchen_run(client, monkeypatch):
    monkeypatch.setattr(
        claude_client, "generate_container_spec", lambda *a, **k: copy.deepcopy(SAMPLE_CONTAINER_SPEC_WITH_KITCHEN)
    )
    resp = client.post("/api/prompt", json={"mode": "container", "text": "20ft with kitchen", "current_spec": None})
    assert resp.status_code == 200
    data = resp.get_json()
    assert "kitchen_run" in data["spec"]["plan"]
    assert data["preview_data_uri"].startswith("data:image/png;base64,")

    monkeypatch.setattr(
        claude_client, "generate_container_spec", lambda *a, **k: copy.deepcopy(SAMPLE_CONTAINER_SPEC_NO_KITCHEN)
    )
    resp2 = client.post("/api/prompt", json={"mode": "container", "text": "40ft office, no kitchen", "current_spec": None})
    assert resp2.status_code == 200
    data2 = resp2.get_json()
    assert "plan" not in data2["spec"]
    assert data2["preview_data_uri"].startswith("data:image/png;base64,")


# ---------------------------------------------------------------------------
# 4. /api/download works purely from a client-supplied spec (no prior server state)
# ---------------------------------------------------------------------------
def test_download_standalone_from_client_spec(client):
    resp = client.post("/api/download", json={"mode": "container", "spec": SAMPLE_CONTAINER_SPEC_WITH_KITCHEN})
    assert resp.status_code == 200
    assert len(resp.data) > 0
    assert b"MTEXT" in resp.data


# ---------------------------------------------------------------------------
# 5. Memory: add/remove preference, auto-logged history
# ---------------------------------------------------------------------------
def test_memory_preferences_and_history(client, monkeypatch):
    resp = client.post("/api/memory/preference", json={"text": "always use 150mm wall thickness"})
    assert resp.status_code == 200
    mem = resp.get_json()
    assert "always use 150mm wall thickness" in mem["preferences"]

    context = memory.build_context_block("floorplan")
    assert "150mm wall thickness" in context

    got = client.get("/api/memory").get_json()
    index = got["preferences"].index("always use 150mm wall thickness")
    resp2 = client.delete(f"/api/memory/preference/{index}")
    assert "always use 150mm wall thickness" not in resp2.get_json()["preferences"]

    monkeypatch.setattr(claude_client, "generate_floorplan_spec", lambda *a, **k: copy.deepcopy(SAMPLE_FLOORPLAN_SPEC))
    client.post("/api/prompt", json={"mode": "floorplan", "text": "a living room", "current_spec": None})
    mem_after = client.get("/api/memory").get_json()
    assert len(mem_after["history"]["floorplan"]) == 1
    assert "walls" in mem_after["history"]["floorplan"][0]

    resp3 = client.post("/api/memory/clear", json={})
    cleared = resp3.get_json()
    assert cleared["preferences"] == []
    assert cleared["history"]["floorplan"] == []


# ---------------------------------------------------------------------------
# 6. Auth gate: unauthenticated rejected, correct/incorrect password
# ---------------------------------------------------------------------------
def test_auth_gate(client, monkeypatch):
    monkeypatch.setattr(auth, "APP_PASSWORD", "letmein")

    resp = client.get("/api/memory")
    assert resp.status_code == 401

    bad = client.post("/login", data={"password": "wrong"})
    assert bad.status_code == 200
    assert b"Incorrect password" in bad.data

    still_out = client.get("/api/memory")
    assert still_out.status_code == 401

    good = client.post("/login", data={"password": "letmein"}, follow_redirects=False)
    assert good.status_code == 302

    now_in = client.get("/api/memory")
    assert now_in.status_code == 200


# ---------------------------------------------------------------------------
# 7. Dashboard: save/list/reopen/update/delete
# ---------------------------------------------------------------------------
def test_dashboard_save_list_reopen_update_delete(client):
    saved = client.post(
        "/api/designs",
        json={"mode": "floorplan", "title": "EMG Client Layout", "spec": SAMPLE_FLOORPLAN_SPEC},
    ).get_json()
    design_id = saved["id"]
    assert saved["title"] == "EMG Client Layout"

    index = client.get("/api/designs").get_json()
    assert any(d["id"] == design_id for d in index)
    assert "spec" not in index[0]  # index is metadata only

    full = client.get(f"/api/designs/{design_id}").get_json()
    assert full["spec"] == SAMPLE_FLOORPLAN_SPEC
    assert full["preview_data_uri"].startswith("data:image/png;base64,")

    updated_spec = copy.deepcopy(SAMPLE_FLOORPLAN_SPEC)
    updated_spec["rooms"][0]["name"] = "Renamed Room"
    resaved = client.post(
        "/api/designs",
        json={"mode": "floorplan", "title": "EMG Client Layout v2", "spec": updated_spec, "id": design_id},
    ).get_json()
    assert resaved["id"] == design_id  # updates in place, not a new record

    index_after = client.get("/api/designs").get_json()
    assert len(index_after) == 1
    assert index_after[0]["title"] == "EMG Client Layout v2"

    deleted = client.delete(f"/api/designs/{design_id}")
    assert deleted.get_json()["deleted"] is True
    assert client.get("/api/designs").get_json() == []
    assert client.get(f"/api/designs/{design_id}").status_code == 404


# ---------------------------------------------------------------------------
# 8. Open-ended extras: an element with no first-class field is drawn as a
#    visible labeled placeholder zone rather than silently dropped.
# ---------------------------------------------------------------------------
def test_additional_elements_placeholder(client, monkeypatch):
    spec = {
        "container": {"length_mm": 6058, "width_mm": 2438, "height_mm": 2896},
        "additional_elements": [
            {"label": "wood stove", "approx_position": [3000, 1200], "approx_size_mm": [800, 800]},
        ],
    }
    monkeypatch.setattr(claude_client, "generate_container_spec", lambda *a, **k: copy.deepcopy(spec))
    resp = client.post("/api/prompt", json={"mode": "container", "text": "add a wood stove", "current_spec": None})
    assert resp.status_code == 200

    dl = client.post("/api/download", json={"mode": "container", "spec": resp.get_json()["spec"]})
    assert b"wood stove" in dl.data       # the label is drawn
    assert b"DASHED" in dl.data           # as a dashed placeholder zone


# ---------------------------------------------------------------------------
# 9. Clarification: an underspecified request yields a question, not a guess.
# ---------------------------------------------------------------------------
def test_clarification_question_is_surfaced(client, monkeypatch):
    def ask(*a, **k):
        raise claude_client.ClarificationNeeded("Which wall should the window go on?")

    monkeypatch.setattr(claude_client, "generate_floorplan_spec", ask)
    resp = client.post("/api/prompt", json={"mode": "floorplan", "text": "add a window", "current_spec": None})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data.get("needs_clarification") is True
    assert "wall" in data["question"].lower()
    assert "spec" not in data


# ---------------------------------------------------------------------------
# 10. Validation: a spec that can't fit triggers ONE self-correction; if the
#     correction fixes it we proceed, otherwise a specific error is returned.
# ---------------------------------------------------------------------------
def test_validation_self_correction_recovers(client, monkeypatch):
    bad = {
        "container": {"length_mm": 6058, "width_mm": 2438, "height_mm": 2896},
        "plan": {"kitchen_run": {"depth_mm": 700, "segments": [
            {"label": "counter", "width_mm": 5000}, {"label": "counter", "width_mm": 5000}]}},
    }
    good = copy.deepcopy(SAMPLE_CONTAINER_SPEC_WITH_KITCHEN)
    calls = {"n": 0}

    def gen(text, current_spec=None, context_block="", correction_problems=None):
        calls["n"] += 1
        # First call returns an over-length kitchen run; the correction call
        # (correction_problems set) returns a valid spec.
        return copy.deepcopy(good) if correction_problems else copy.deepcopy(bad)

    monkeypatch.setattr(claude_client, "generate_container_spec", gen)
    resp = client.post("/api/prompt", json={"mode": "container", "text": "cram in two 5m counters", "current_spec": None})
    assert resp.status_code == 200
    assert calls["n"] == 2                      # generated once, self-corrected once
    assert "kitchen_run" in resp.get_json()["spec"]["plan"]


def test_validation_hard_failure_returns_specific_error(client, monkeypatch):
    bad = {
        "container": {"length_mm": 6058, "width_mm": 2438, "height_mm": 2896},
        "plan": {"kitchen_run": {"depth_mm": 700, "segments": [
            {"label": "counter", "width_mm": 5000}, {"label": "counter", "width_mm": 5000}]}},
    }
    monkeypatch.setattr(claude_client, "generate_container_spec", lambda *a, **k: copy.deepcopy(bad))
    resp = client.post("/api/prompt", json={"mode": "container", "text": "two 5m counters", "current_spec": None})
    assert resp.status_code == 422
    msg = resp.get_json()["error"]
    assert "kitchen run" in msg.lower() and "interior" in msg.lower()  # names the actual conflict


# ---------------------------------------------------------------------------
# 11. Regression: a container with back-wall windows must render (guards the
#     label_boxes ordering bug that 500'd any windowed design).
# ---------------------------------------------------------------------------
def test_container_with_windows_renders(client):
    spec = {
        "container": {"length_mm": 6058, "width_mm": 2438, "height_mm": 2896},
        "plan": {
            "title": "Plan",
            "kitchen_run": {"depth_mm": 600, "segments": [{"label": "Sink", "width_mm": 680}]},
            "windows": [{"width_mm": 1000, "height_mm": 1000, "position_from_left_mm": 2500}],
        },
    }
    dl = client.post("/api/download", json={"mode": "container", "spec": spec})
    assert dl.status_code == 200
    assert b"W:1000*1000mm" in dl.data
