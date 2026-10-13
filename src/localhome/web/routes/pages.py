"""The pages themselves: the home page, the admin panel, the phone's
home-screen manifest, and the board's clock the pages check theirs against."""
from __future__ import annotations

import socket

from flask import Flask, jsonify, render_template

from localhome.config import LocalHomeConfig
from localhome.services import panel_settings
from localhome.services import system_status
from localhome.services.system_clock import server_time


def register(app: Flask) -> None:
    """The pages and what they need to open."""
    def site_name() -> str:
        """What this installation calls itself: the admin panel's setting,
        else config.yaml's, else the translated default. Read per page
        render (pages are rare; the API polls don't touch this), so a new
        name is there on the next reload without a restart."""
        settings: panel_settings.PanelSettingsStore = app.config["PANEL_SETTINGS"]
        chosen = settings.get().get("site_name")
        if chosen:
            return chosen
        config: LocalHomeConfig = app.config["LOCALHOME_CONFIG"]
        from_config = (config.raw.get("web") or {}).get("site_name")
        return from_config or app.config["TRANSLATIONS"]["app"]["title"]

    app.config["SITE_NAME"] = site_name

    @app.route("/site.webmanifest")
    def site_webmanifest():
        # Built here rather than served from static/, so the name under the
        # icon on a phone's home screen is the one set in the panel.
        name = site_name()
        return jsonify(
            name=name,
            short_name=name,
            start_url="/",
            scope="/",
            display="standalone",
            background_color="#0b0d13",
            theme_color="#0b0d13",
            icons=[
                {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                {"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"},
            ],
        )

    @app.route("/")
    def home():
        # The home page - glance, tweak, plan; see static/home/.
        config: LocalHomeConfig = app.config["LOCALHOME_CONFIG"]
        return render_template("home.html", site_name=site_name(), t=app.config["TRANSLATIONS"],
                               lang=config.web_language)

    @app.route("/api/server-time")
    def api_server_time():
        # The clock both pages show - the server's, not the browser's, so
        # a wrongly-set board timezone is visible instead of silently
        # shifting every schedule slot. See services/system_clock.py.
        # The board's start time goes out with the clock both pages already
        # poll: it's what tells a page that a days-old reading was simply
        # never heard (the board was off) rather than a sensor gone quiet.
        return jsonify(ok=True, app_version=app.config["ASSET_VERSION"],
                       clock_synced=system_status.clock_synced_fast(),
                       **system_status.boot_times(), **server_time())

    @app.route("/climate-admin")
    def climate_admin_page():
        config: LocalHomeConfig = app.config["LOCALHOME_CONFIG"]
        return render_template(
            "climate_admin.html",
            site_name=site_name(),
            t=app.config["TRANSLATIONS"],
            lang=config.web_language,
            # The address the Shellys' MQTT setting points at - see the
            # Devices section's guide to the Shelly app.
            board_host=f"{socket.gethostname()}.local",
        )
