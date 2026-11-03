"""Versioned API paths (roadmap I3).

Every ``/api/...`` route is served at ``/api/v1/...``, which is the documented, stable
contract: the OpenAPI schema lists only the versioned paths. Requests to ``/api/v1/``
are routed to the same handlers by rewriting the path before routing, so routers need no
changes. The unversioned paths stay as aliases for the bundled web pages, an unknown
version is not found, and every API response says which version served it. A breaking
change gets ``/api/v2`` while ``v1`` keeps working.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

API_VERSION = "v1"
VERSION_HEADER = "X-Syntera-API-Version"
_PREFIX = f"/api/{API_VERSION}/"


class VersionedPaths:
    """ASGI middleware: ``/api/v1/x`` is served by ``/api/x``; API responses get a header."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if path.startswith(_PREFIX):
            rewritten = "/api/" + path.removeprefix(_PREFIX)
            scope = {**scope, "path": rewritten, "raw_path": rewritten.encode("utf-8")}

        async def send_with_version(message):
            if message["type"] == "http.response.start":
                headers = [*message.get("headers", []),
                           (VERSION_HEADER.lower().encode(), API_VERSION.encode())]  # fmt: skip
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_version)


def versioned_path(path: str) -> str:
    if path.startswith("/api/") and not re.match(r"^/api/v\d+/", path):
        return _PREFIX + path.removeprefix("/api/")
    return path


def add_versioned_routes(app: FastAPI) -> None:
    """Serve ``/api/v1/`` and document only versioned paths in OpenAPI."""
    app.add_middleware(VersionedPaths)

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            schema["paths"] = {versioned_path(p): ops for p, ops in schema["paths"].items()}
            schema["info"]["x-api-version"] = API_VERSION
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
