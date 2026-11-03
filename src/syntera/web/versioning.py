"""Versioned API paths (roadmap I3).

Every ``/api/...`` route is served at ``/api/v1/...``, which is the documented, stable
contract (the OpenAPI schema lists only the versioned paths). The unversioned paths stay
as aliases for the bundled web pages. Every API response says which version served it.
A breaking change gets ``/api/v2`` while ``v1`` keeps working.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.routing import APIRoute

API_VERSION = "v1"
VERSION_HEADER = "X-Syntera-API-Version"


def add_versioned_routes(app: FastAPI) -> None:
    """Serve every ``/api/`` route under ``/api/v1/`` too; only v1 appears in OpenAPI."""
    for route in list(app.router.routes):
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
            continue
        if route.path.startswith(f"/api/{API_VERSION}/"):
            continue
        app.router.add_api_route(
            f"/api/{API_VERSION}/" + route.path.removeprefix("/api/"),
            route.endpoint,
            methods=sorted(route.methods),
            status_code=route.status_code,
            response_model=route.response_model,
            response_class=route.response_class,
            name=f"{API_VERSION}_{route.name}",
            include_in_schema=route.include_in_schema,
        )
        route.include_in_schema = False

    @app.middleware("http")
    async def version_header(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers[VERSION_HEADER] = API_VERSION
        return response
