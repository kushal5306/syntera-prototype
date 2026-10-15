from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from syntera.cli import main
from syntera.web.app import create_app

PROJECT_ROOT = Path(__file__).parents[2]
CONFIG = str(PROJECT_ROOT / "examples" / "demo_skid.yaml")
MANUAL = PROJECT_ROOT / "examples" / "manual_baseline.yaml"


def test_cli_dry_run_logs_review_and_compute_time(tmp_path):
    log = str(tmp_path / "time_log.jsonl")
    output = tmp_path / "demo"
    mark = ["time", "--log", log, "--task", "one-line", "--phase", "review"]
    assert main([mark[0], "start", *mark[1:]]) == 0
    demo = ["demo", "--config", CONFIG, "--output", str(output)]
    assert main([*demo, "--time-log", log, "--task", "one-line"]) == 0
    assert main([mark[0], "stop", *mark[1:], "--note", "approved"]) == 0

    assert main(["kpi1", "--log", log, "--baseline", str(MANUAL), "--output", str(output)]) == 0
    report = json.loads((output / "kpi1_report.json").read_text(encoding="utf-8"))
    assert report["log_valid"] is True
    one_line = report["tasks"][0]
    assert one_line["task"] == "one-line" and one_line["measured"] is True
    assert {phase["phase"] for phase in one_line["ai_phases"]} == {"generate", "review"}
    assert 0 < one_line["ratio"] < 1
    # Two benchmark tasks have no Syntera time yet, so the claim is withheld.
    assert report["unmeasured_tasks"] == ["three-lines", "three-lines-fea"]
    assert report["kpi1_met"] is False


def test_time_log_without_task_is_an_error(tmp_path):
    log = str(tmp_path / "time_log.jsonl")
    demo = ["demo", "--config", CONFIG, "--output", str(tmp_path / "demo")]
    assert main([*demo, "--time-log", log]) == 2


def test_web_logs_review_and_routing_time(tmp_path):
    client = TestClient(create_app(Path(CONFIG), tmp_path / "web", MANUAL))
    started = client.post(
        "/api/time", json={"task": "one-line", "phase": "review", "event": "start"}
    )
    assert started.status_code == 200
    assert [event["phase"] for event in started.json()["running"]] == ["review"]
    assert (
        client.post("/api/time", json={"task": "a b", "phase": "x", "event": "start"}).status_code
        == 422
    )

    config = client.get("/api/config").json()
    assert client.post("/api/route?task=one-line", json=config).status_code == 200
    stopped = client.post(
        "/api/time", json={"task": "one-line", "phase": "review", "event": "stop"}
    )
    assert stopped.json()["running"] == []
    assert stopped.json()["errors"] == []

    kpi1 = client.get("/api/kpi1").json()
    assert kpi1["log_valid"] is True
    phases = {phase["phase"] for phase in kpi1["tasks"][0]["ai_phases"]}
    assert phases == {"generate", "review"}
    assert (tmp_path / "web" / "time_log.jsonl").is_file()


def test_kpi1_endpoint_needs_a_manual_baseline(tmp_path):
    client = TestClient(create_app(Path(CONFIG), tmp_path / "web"))
    assert client.get("/api/kpi1").status_code == 404
