"""Optional HTTP Basic Auth for the whole dashboard/API.

Off by default - this project assumes you're running it on a trusted
LAN, same as the rest of the framework. Turn it on (`web.auth` in
config.yaml) for anything reachable beyond that, but see
docs/configuration.md#web-auth-optional first: Basic Auth over plain
HTTP is better than nothing, not a substitute for TLS if this is ever
reachable off your LAN.
"""
from __future__ import annotations

import hmac

from flask import Flask, Response, request
from werkzeug.security import check_password_hash

from localhome.util.jsonfile import load_json


def register_auth(app: Flask, secrets_file: str) -> None:
    credentials = load_json(secrets_file)
    username = credentials["username"]
    password_hash = credentials.get("password_hash")
    password = credentials.get("password")
    if not password_hash and not password:
        raise ValueError(f"{secrets_file} needs either 'password_hash' (preferred) or 'password'")

    def is_valid(candidate_user: str, candidate_password: str) -> bool:
        if not hmac.compare_digest(candidate_user, username):
            return False
        if password_hash:
            return check_password_hash(password_hash, candidate_password)
        return hmac.compare_digest(candidate_password, password)

    @app.before_request
    def _require_auth():
        auth = request.authorization
        if auth and auth.username and auth.password and is_valid(auth.username, auth.password):
            return None
        return Response(
            "Authentication required.",
            401,
            {"WWW-Authenticate": 'Basic realm="LocalHome"'},
        )
