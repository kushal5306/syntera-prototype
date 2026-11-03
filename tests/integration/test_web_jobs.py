"""Routing, FEA and optimisation as background jobs over HTTP (I1)."""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from syntera.web.app import create_app

PROJECT = Path(__file__).parents[2]
FIXED = PROJECT / "examples" / "demo_skid.yaml"


def _finish(client, job_id):
    return client.app.state.jobs.wait(job_id, timeout=600)


def test_route_design_and_optimise_jobs(tmp_path):
    client = TestClient(create_app(FIXED, tmp_path / "web"))
    submitted = client.post("/api/jobs", json={"kind": "route-design"})
    assert submitted.status_code == 202
    job = submitted.json()
    assert job["status"] in {"queued", "running"} and job["status_url"] == f"/api/jobs/{job['id']}"
    _finish(client, job["id"])
    done = client.get(job["status_url"]).json()
    assert done["status"] == "succeeded", done["error"]
    assert done["result"]["overall_pass"] is True
    assert done["result"]["source"].startswith("exact geometry checks")

    factor = {"kind": "bend_radius_factor", "line": "main", "low": 1.5, "high": 2.0, "step": 0.5}
    study = {"variables": [factor], "objectives": ["length"], "budget": 2}
    optimised = client.post("/api/jobs", json={"kind": "optimise", "payload": study}).json()
    _finish(client, optimised["id"])
    result = client.get(f"/api/jobs/{optimised['id']}").json()
    assert result["status"] == "succeeded" and result["result"]["evaluated"] == 2

    bad = client.post("/api/jobs", json={"kind": "optimise", "payload": {"budget": 2}}).json()
    _finish(client, bad["id"])
    assert client.get(f"/api/jobs/{bad['id']}").json()["status"] == "failed"
    assert [j["id"] for j in client.get("/api/jobs").json()] == [
        job["id"], optimised["id"], bad["id"],
    ]  # fmt: skip
    assert client.post("/api/jobs", json={"kind": "mine-bitcoin"}).status_code == 422
    assert client.get("/api/jobs/job-123456").status_code == 404


def test_fea_job_solves_the_latest_run(tmp_path):
    client = TestClient(create_app(FIXED, tmp_path / "web"))
    early = client.post("/api/jobs", json={"kind": "fea"}).json()
    _finish(client, early["id"])
    failed = client.get(f"/api/jobs/{early['id']}").json()
    assert failed["status"] == "failed" and "route the design" in failed["error"]

    config = client.get("/api/config").json()
    assert client.post("/api/route", json=config).status_code == 200
    job = client.post("/api/jobs", json={"kind": "fea"}).json()
    _finish(client, job["id"])
    done = client.get(f"/api/jobs/{job['id']}").json()
    if shutil.which("ccx") is None:
        assert done["status"] == "failed" and "not installed" in done["error"]
    else:
        assert done["status"] == "succeeded", done["error"]
        assert done["result"]["source"].startswith("FEA acceptance")
        assert isinstance(done["result"]["overall_pass"], bool)
