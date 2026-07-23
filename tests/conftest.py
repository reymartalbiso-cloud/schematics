import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import storage


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    """Every test gets its own throwaway local-file KV store, Upstash disabled."""
    monkeypatch.setattr(storage, "_LOCAL_DIR", tmp_path)
    monkeypatch.setattr(storage, "_LOCAL_FILE", tmp_path / "kv_store.json")
    monkeypatch.setattr(storage, "UPSTASH_URL", "")
    monkeypatch.setattr(storage, "UPSTASH_TOKEN", "")
    yield


@pytest.fixture
def flask_app():
    import app as app_module

    app_module.app.config.update(TESTING=True)
    return app_module.app


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


SAMPLE_FLOORPLAN_SPEC = {
    "meta": {"units": "mm"},
    "walls": [{"id": "w1", "start": [0, 0], "end": [6000, 0], "thickness": 200}],
    "openings": [
        {"type": "door", "wall_id": "w1", "position_along_wall": 1200, "width": 900, "swing": "left"},
        {"type": "window", "wall_id": "w1", "position_along_wall": 3000, "width": 1200},
    ],
    "rooms": [{"name": "Living Room", "area_sqm": 24.0, "label_position": [3000, 2000]}],
    "dimensions": [{"start": [0, 0], "end": [6000, 0], "offset": -400}],
}

SAMPLE_CONTAINER_SPEC_WITH_KITCHEN = {
    "container": {"length_mm": 6058, "width_mm": 2438, "height_mm": 2896, "model": "20ft Container Home"},
    "plan": {
        "title": "Plan View  1:30",
        "kitchen_run": {
            "depth_mm": 700,
            "segments": [
                {"label": "hob", "width_mm": 700},
                {"label": "counter", "width_mm": 1820},
                {"label": "sink", "width_mm": 680},
            ],
        },
        "sliding_door": {"width_mm": 2000, "position_from_left_mm": 2029},
    },
}

SAMPLE_CONTAINER_SPEC_NO_KITCHEN = {
    "container": {"length_mm": 12192, "width_mm": 2438, "height_mm": 2896, "model": "40ft Container Office"},
}
