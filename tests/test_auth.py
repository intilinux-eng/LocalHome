import base64
import json

from flask import Flask
from werkzeug.security import generate_password_hash

from localhome.web.auth import register_auth


def _basic_header(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _build_app(tmp_path, credentials: dict) -> Flask:
    secrets_path = tmp_path / "secrets_web.json"
    secrets_path.write_text(json.dumps(credentials), encoding="utf-8")

    app = Flask(__name__)
    register_auth(app, str(secrets_path))

    @app.route("/protected")
    def protected():
        return "ok"

    return app


def test_request_without_credentials_is_rejected(tmp_path):
    app = _build_app(tmp_path, {"username": "admin", "password": "secret"})
    client = app.test_client()

    resp = client.get("/protected")

    assert resp.status_code == 401
    assert "WWW-Authenticate" in resp.headers


def test_correct_plain_password_is_accepted(tmp_path):
    app = _build_app(tmp_path, {"username": "admin", "password": "secret"})
    client = app.test_client()

    resp = client.get("/protected", headers=_basic_header("admin", "secret"))

    assert resp.status_code == 200


def test_wrong_password_is_rejected(tmp_path):
    app = _build_app(tmp_path, {"username": "admin", "password": "secret"})
    client = app.test_client()

    resp = client.get("/protected", headers=_basic_header("admin", "wrong"))

    assert resp.status_code == 401


def test_correct_hashed_password_is_accepted(tmp_path):
    app = _build_app(tmp_path, {"username": "admin", "password_hash": generate_password_hash("secret")})
    client = app.test_client()

    resp = client.get("/protected", headers=_basic_header("admin", "secret"))

    assert resp.status_code == 200


def test_unknown_username_is_rejected(tmp_path):
    app = _build_app(tmp_path, {"username": "admin", "password": "secret"})
    client = app.test_client()

    resp = client.get("/protected", headers=_basic_header("someone-else", "secret"))

    assert resp.status_code == 401
