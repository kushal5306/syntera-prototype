"""Generated sheets can be viewed and downloaded from the web app (G3)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from syntera.web.app import create_app

PROJECT = Path(__file__).parents[2]
FIXED = PROJECT / "examples" / "demo_skid.yaml"


def test_sheet_from_the_latest_run_is_viewable_and_downloadable(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "no-oda"))  # no DWG writer in this test
    client = TestClient(create_app(FIXED, tmp_path / "web"))
    assert client.post("/api/sheets").status_code == 409  # nothing routed yet
    assert client.get("/api/sheets/capabilities").json() == {"dxf": True, "pdf": True,
                                                              "dwg": False}  # fmt: skip

    config = client.get("/api/config").json()
    assert client.post("/api/route", json=config).status_code == 200
    sheet = client.post("/api/sheets").json()
    assert sheet["id"] == 1 and sheet["assurance_in_title_block"] == "PASS"
    names = [d["name"] for d in sheet["downloads"]]
    assert names == ["sheet.dxf", "sheet.pdf", "sheet.json"]

    pdf = client.get("/api/sheets/1/sheet.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    dxf = client.get("/api/sheets/1/sheet.dxf")
    assert dxf.status_code == 200 and b"SYNTERA" in dxf.content
    assert client.get("/api/sheets/1/sheet.dwg").status_code == 404
    assert client.get("/api/sheets/1/../../secret").status_code == 404
    assert client.get("/api/sheets/x/sheet.dxf").status_code == 404

    # the sheet is filed in the drawings store, so the drawings page shows it
    drawing = sheet["drawing_id"]
    assert sheet["viewer"] == f"/drawings#{drawing}"
    summary = client.get(f"/api/drawings/{drawing}/summary.json").json()
    assert summary["filename"] == "sheet-1.dxf"
    assert [s["id"] for s in client.get("/api/sheets").json()] == [1]
    assert client.post("/api/sheets").json()["id"] == 2
