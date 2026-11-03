"""Parameter editing with a diff view (G2) and design history (G4) over HTTP."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from syntera.web.app import create_app

PROJECT = Path(__file__).parents[2]
FIXED = PROJECT / "examples" / "demo_skid.yaml"


def _edit(path, value):
    return {"description": f"set {path}", "edits": [
        {"op": "set_parameter", "path": path, "value": value}]}  # fmt: skip


def test_editing_previews_applies_and_regenerates(tmp_path):
    client = TestClient(create_app(FIXED, tmp_path / "web"))
    view = client.get("/api/design").json()
    assert view["version"] == 1
    values = {v["path"]: v for v in view["values"]}
    assert values["lines.main.tube.minimum_clearance"]["editable"] is True
    assert values["lines.main.tube.minimum_clearance"]["source"] == "user"
    assert values["parameters.routing_cost_weights.port_direction"]["editable"] is False

    preview = client.post(
        "/api/design/preview", json={"changes": _edit("lines.main.tube.minimum_clearance", 25)}
    ).json()
    assert preview["valid"] is True
    assert preview["diff"]["values"] == [{"path": "lines.main.tube.minimum_clearance",
                                          "before": 20.0, "after": 25}]  # fmt: skip
    assert client.get("/api/design").json()["version"] == 1  # preview applies nothing

    bad = client.post(
        "/api/design/preview", json={"changes": _edit("lines.main.tube.minimum_bend_radius", 1)}
    ).json()
    assert bad["valid"] is False and "invalid design" in bad["error"]
    outputs = {"description": "inject", "edits": [{"op": "set_routes", "routes": []}]}
    assert client.post("/api/design/preview", json={"changes": outputs}).status_code == 422
    assert client.post("/api/design/apply", json={"changes": outputs}).status_code == 422

    applied = client.post(
        "/api/design/apply", json={"changes": _edit("lines.main.tube.minimum_clearance", 25)}
    ).json()
    assert applied["version"] == 2 and applied["verdicts"] is None
    assert applied["revert"]["edits"][0]["op"] == "set_parameter"
    now = {v["path"]: v for v in client.get("/api/design").json()["values"]}
    assert now["lines.main.tube.minimum_clearance"]["value"] == 25

    weights = _edit("parameters.routing_cost_weights.bend", 90)
    body = {"changes": weights, "regenerate": True}
    regenerated = client.post("/api/design/apply", json=body).json()
    assert regenerated["version"] == 3
    assert regenerated["verdicts"]["overall_pass"] is True
    assert regenerated["verdicts"]["source"].startswith("exact geometry checks")
    assert client.get("/api/design").json()["lines"][0]["routed"] is True


def test_history_keeps_every_version_and_compares_any_two(tmp_path):
    client = TestClient(create_app(FIXED, tmp_path / "web"))
    client.post(
        "/api/design/apply", json={"changes": _edit("lines.main.tube.minimum_clearance", 25)}
    )
    client.post("/api/design/apply", json={"changes": _edit("parameters.voxel_resolution", 10)})
    versions = client.get("/api/design/versions").json()
    assert [(v["number"], v["parent"]) for v in versions] == [(1, None), (2, 1), (3, 2)]
    assert client.get("/api/design/versions/2").json()["version"] == 2
    assert client.get("/api/design/versions/9").status_code == 404
    diff = client.get("/api/design/compare", params={"a": 1, "b": 3}).json()["diff"]
    assert [v["path"] for v in diff["values"]] == [
        "lines.main.tube.minimum_clearance", "parameters.voxel_resolution",
    ]  # fmt: skip
    same = client.get("/api/design/compare", params={"a": 2, "b": 2}).json()["diff"]
    assert same["values"] == [] and same["rationale"] == []
    assert client.get("/api/design/compare", params={"a": 1, "b": 7}).status_code == 404
    # versions persist across a server restart
    again = TestClient(create_app(FIXED, tmp_path / "web"))
    assert len(again.get("/api/design/versions").json()) == 3
    assert again.get("/api/design").json()["version"] == 3


def test_design_page_is_served_and_display_only(tmp_path):
    client = TestClient(create_app(FIXED, tmp_path / "web"))
    html = client.get("/design").text
    assert '<script src="/assets/design.js"></script>' in html
    source = client.get("/assets/design.js").text
    for endpoint in ("/api/design", "/api/design/preview", "/api/design/apply",
                     "/api/design/versions", "/api/design/compare"):  # fmt: skip
        assert endpoint in source
    for forbidden in ("eval(", "innerHTML", "new Function"):
        assert forbidden not in source
    assert client.get("/assets/design.css").status_code == 200
