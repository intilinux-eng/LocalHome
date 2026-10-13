"""Every HTTP endpoint, one module per area - each with its register(app),
called once by create_app(). Shared request helpers in common.py."""
from __future__ import annotations

from flask import Flask

from localhome.web.routes import admin, climate, devices, pages, plant, system


def register_routes(app: Flask) -> None:
    for area in (pages, system, devices, plant, climate, admin):
        area.register(app)
