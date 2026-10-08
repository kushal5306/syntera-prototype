from __future__ import annotations

import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from syntera.web.app import create_app


def test_web_app_previews_routes_and_serves_outputs(tmp_path):
    project_root = Path(__file__).parents[2]
    app = create_app(project_root / "examples" / "demo_skid.yaml", tmp_path / "web")
    client = TestClient(app)

    assert client.get("/api/health").json() == {
        "status": "ok",
        "engine": "deterministic",
    }
    assert "Syntera" in client.get("/").text
    assert client.get("/assets/vendor/three.min.js").status_code == 200
    assert client.get("/api/capabilities").json() == {"calculix": shutil.which("ccx") is not None}
    config = client.get("/api/config").json()

    preview = client.post("/api/preview", json=config)
    assert preview.status_code == 200
    assert len(preview.json()["meshes"]) == len(config["obstacles"]) + 2
    assert all(mesh["triangle_count"] > 0 for mesh in preview.json()["meshes"])

    routed = client.post("/api/route", json=config)
    assert routed.status_code == 200
    payload = routed.json()
    assert payload["assurance"]["overall_pass"] is True
    assert payload["meshes"][-1]["id"] == "routed-tube"
    assert payload["route"]["found"] is True
    assert payload["config"]["tube"] == config["tube"]
    fea = payload["fea"]
    assert fea["manifest"]["generated"] is True
    if shutil.which("ccx"):
        assert fea["solved"] is True
        assert fea["acceptance"]["overall_pass"] is True
        assert len(fea["nodes"]) == fea["manifest"]["mesh"]["nodes"]
        assert len(fea["elements"]) == fea["manifest"]["mesh"]["elements"]
        for case in fea["cases"]:
            assert len(case["stress"]) == len(fea["elements"])
            assert max(case["stress"]) > 0
    else:
        assert fea["solved"] is False
        assert "not installed" in fea["diagnostic"]
    downloads = {item["name"] for item in payload["downloads"]}
    assert "routed_assembly.step" in downloads
    step = client.get("/api/download/routed_assembly.step")
    assert step.status_code == 200
    assert b"ISO-10303-21" in step.content[:100]


def test_web_app_rejects_unknown_download(tmp_path):
    project_root = Path(__file__).parents[2]
    client = TestClient(create_app(project_root / "examples" / "demo_skid.yaml", tmp_path / "web"))
    assert client.get("/api/download/not-a-file.step").status_code == 404
