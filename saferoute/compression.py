"""Gzip responses for slow connections.

During a disaster people are often on weak mobile data. Road data, scripts and pages are text, and
gzip shrinks them to roughly a quarter of their size, so the map loads several times faster.
"""

from __future__ import annotations

import gzip

from flask import Flask, Response, request

COMPRESSIBLE = ("text/", "application/json", "application/javascript", "application/manifest+json", "image/svg+xml")
MIN_BYTES = 1024


def wants_gzip() -> bool:
    accepted = request.headers.get("Accept-Encoding", "").lower()
    return any(part.split(";")[0].strip() == "gzip" for part in accepted.split(","))


def add_compression(app: Flask) -> None:
    @app.after_request
    def compress(response: Response) -> Response:
        if (
            response.status_code != 200
            or "Content-Encoding" in response.headers
            or not (response.mimetype or "").startswith(COMPRESSIBLE)
        ):
            return response
        response.vary.add("Accept-Encoding")
        if not wants_gzip():
            return response
        response.direct_passthrough = False  # static files stream by default; read them in
        body = response.get_data()
        if len(body) < MIN_BYTES:
            return response
        response.set_data(gzip.compress(body, compresslevel=6))
        response.headers["Content-Encoding"] = "gzip"
        return response
