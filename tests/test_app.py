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
    assert b"DIMENSION" in body


# ---------------------------------------------------------------------------
# 2. Edit prompt correctly passes current_spec through without dropping it
# ---------------------------------------------------------------------------
def test_floorplan_edit_preserves_current_spec_context(client, monkeypatch):
    captured = {}

    def fake_generate(text, current_spec=None, context_block=""):
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
