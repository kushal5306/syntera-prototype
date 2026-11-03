"""Versioned API paths and the v1 contract snapshot (I3).

``tests/contracts/api_v1.json`` is the v1 contract: every documented endpoint and the
fields of every JSON request body. Changing it is a deliberate API change; a breaking
change needs a new version instead.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from syntera.web.app import create_app
from syntera.web.versioning import API_VERSION, VERSION_HEADER

PROJECT = Path(__file__).parents[2]
CONTRACT = json.loads((Path(__file__).parents[1] / "contracts" / "api_v1.json").read_text())
FIXED = PROJECT / "examples" / "demo_skid.yaml"


def _spec(tmp_path):
    client = TestClient(create_app(FIXED, tmp_path / "web"))
    return client, client.get("/openapi.json").json()


def _endpoints(spec):
    found = set()
    for path, operations in spec["paths"].items():
        for method in operations:
            found.add((method.upper(), path))
    return found


def test_openapi_documents_exactly_the_v1_contract(tmp_path):
    _, spec = _spec(tmp_path)
    documented = _endpoints(spec)
    contracted = {(item["method"], item["path"]) for item in CONTRACT["endpoints"]}
    assert documented == contracted, {
        "missing from the API": sorted(contracted - documented),
        "not in the contract": sorted(documented - contracted),
    }
    assert all(path.startswith(f"/api/{API_VERSION}/") for _, path in documented)


def test_request_bodies_match_the_contract(tmp_path):
    _, spec = _spec(tmp_path)
    schemas = spec["components"]["schemas"]
    for key, fields in CONTRACT["request_bodies"].items():
        method, path = key.split(" ", 1)
        body = spec["paths"][path][method.lower()]["requestBody"]
        schema = body["content"]["application/json"]["schema"]
        name = schema["$ref"].rsplit("/", 1)[-1]
        assert sorted(schemas[name]["properties"]) == fields, key


def test_versioned_and_alias_paths_answer_alike_with_a_version_header(tmp_path):
    client, _ = _spec(tmp_path)
    for path in ("health", "capabilities", "config", "design", "drawings/capabilities"):
        versioned = client.get(f"/api/{API_VERSION}/{path}")
        alias = client.get(f"/api/{path}")
        assert versioned.status_code == alias.status_code == 200, path
        assert versioned.json() == alias.json()
        assert versioned.headers[VERSION_HEADER] == API_VERSION
    assert client.get("/api/v2/health").status_code == 404
    assert VERSION_HEADER not in client.get("/").headers
