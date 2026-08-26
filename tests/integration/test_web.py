from __future__ import annotations

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
    downloads = {item["name"] for item in payload["downloads"]}
    assert "routed_assembly.step" in downloads
    step = client.get("/api/download/routed_assembly.step")
    assert step.status_code == 200
    assert b"ISO-10303-21" in step.content[:100]


def test_web_app_rejects_unknown_download(tmp_path):
    project_root = Path(__file__).parents[2]
    client = TestClient(create_app(project_root / "examples" / "demo_skid.yaml", tmp_path / "web"))
    assert client.get("/api/download/not-a-file.step").status_code == 404
