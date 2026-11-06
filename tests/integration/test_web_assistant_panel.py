"""The assistant panel is served on both pages and only displays server data (G1)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from syntera.web.app import create_app

PROJECT = Path(__file__).parents[2]
SIZED = PROJECT / "examples" / "demo_skid_sized.yaml"


def test_both_pages_load_the_panel_and_it_only_talks_to_the_assistant_api(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = TestClient(create_app(SIZED, tmp_path / "web"))
    for page in ("/expert", "/drawings"):
        html = client.get(page).text
        assert '<script src="/assets/assistant.js"></script>' in html
    script = client.get("/assets/assistant.js")
    assert script.status_code == 200
    source = script.text
    endpoints = {"/api/assistant", "/api/assistant/status", "/api/drawings/"}
    assert all(endpoint in source for endpoint in endpoints)
    # display only: no dynamic code, no HTML injection of server text, no verdict logic
    for forbidden in ("eval(", "innerHTML", "new Function", "overall_pass"):
        assert forbidden not in source
    assert "textContent" in source or "append(" in source
    css = client.get("/assets/styles.css").text
    assert ".assist-panel" in css
    # the status endpoint the panel calls reports when no model is configured
    assert client.get("/api/assistant/status").json() == {"available": False, "provider": None}
