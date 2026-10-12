"""How the pages travel: browser caching and gzip.

Caching - every static file the pages load carries the dashboard's
version in its URL (?v=..., see create_app's url_defaults), which changes
whenever any of them does. Such a URL always means the same bytes, so the
browser keeps it for a year without asking again ("immutable"): opening
the home page no longer re-asks for each of its scripts and styles. A
static URL without that version (the installer's guide) is left as
before - the browser checks it each time. The pages themselves are never
kept: they name the version to load.

Compression - text (pages, scripts, styles, JSON) goes gzipped to a
browser that says it takes it: the home page's ~280 KB become ~70 KB, what
matters for a phone on weak Wi-Fi. Small answers, anything already
encoded, partial ranges and HEAD requests go as they are. A static file
is compressed once per version and kept in memory - the board's CPU does
it once, not at every load.
"""
from __future__ import annotations

import gzip
import threading

from flask import Flask, Response, request

YEAR_SECONDS = 365 * 24 * 3600
# Below this, gzip's header and the CPU aren't worth it.
MIN_BYTES = 1024
COMPRESSIBLE = {
    "text/html", "text/css", "text/plain", "text/javascript", "application/javascript",
    "application/json", "application/manifest+json", "image/svg+xml",
}
LEVEL = 6


class _StaticGzip:
    """Each static file's gzipped bytes, per its path and the version
    they belong to: a new version empties it."""

    def __init__(self):
        self._lock = threading.Lock()
        self._version = None
        self._data: dict[str, bytes] = {}

    def get(self, version: str, path: str, raw: bytes) -> bytes:
        with self._lock:
            if version != self._version:
                self._version, self._data = version, {}
            packed = self._data.get(path)
        if packed is None:
            packed = gzip.compress(raw, compresslevel=LEVEL, mtime=0)
            with self._lock:
                self._data[path] = packed
        return packed


def _accepts_gzip() -> bool:
    return any(part.split(";")[0].strip() == "gzip"
               for part in request.headers.get("Accept-Encoding", "").lower().split(","))


def register_transfer(app: Flask, asset_version: str) -> None:
    static_gzip = _StaticGzip()

    @app.after_request
    def _cache_and_compress(response: Response) -> Response:
        is_static = request.endpoint == "static"
        if is_static and response.status_code == 200 and request.args.get("v") == asset_version:
            response.headers["Cache-Control"] = f"public, max-age={YEAR_SECONDS}, immutable"
        elif response.mimetype == "text/html":
            response.headers["Cache-Control"] = "no-cache"
        return _compressed(response, is_static, static_gzip, asset_version)


def _compressed(response: Response, is_static: bool, static_gzip: _StaticGzip, version: str) -> Response:
    if (response.status_code != 200 or request.method == "HEAD" or response.mimetype not in COMPRESSIBLE
            or "Content-Encoding" in response.headers or response.is_streamed and not response.direct_passthrough):
        return response
    response.vary.add("Accept-Encoding")
    if not _accepts_gzip():
        return response
    response.direct_passthrough = False
    raw = response.get_data()
    if len(raw) < MIN_BYTES:
        return response
    packed = static_gzip.get(version, request.path, raw) if is_static else gzip.compress(raw, compresslevel=LEVEL)
    response.set_data(packed)
    response.headers["Content-Encoding"] = "gzip"
    # Another representation of the same file: its ETag only weakly equal -
    # a browser's If-None-Match still gets its 304.
    etag, weak = response.get_etag()
    if etag and not weak:
        response.set_etag(etag, weak=True)
    return response
