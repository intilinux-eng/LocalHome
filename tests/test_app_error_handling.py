"""Exercises the app-wide error handler in web/app.py using Flask's real
test client against a real (minimal) app - no live server, no network.

Every route here returns JSON, and the frontend's fetch().then(r =>
r.json()) assumes that shape even on failure - Flask's own default for an
uncaught exception is an HTML page, which breaks that silently instead of
surfacing a clear error. This is the backstop for a bug nobody
anticipated yet, like the schedule-validation crash this project
actually hit once (see services/thermostat.py's get_target()/get_week()).
"""
from localhome.web.app import create_app


def _minimal_app(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("web:\n  port: 5000\n", encoding="utf-8")
    return create_app(str(config_path))


def test_a_normal_404_is_not_turned_into_a_500(tmp_path):
    app = _minimal_app(tmp_path)
    client = app.test_client()

    res = client.get("/api/this-route-does-not-exist")

    assert res.status_code == 404


def test_an_unhandled_exception_returns_a_clean_json_error_not_html(tmp_path):
    app = _minimal_app(tmp_path)

    @app.route("/api/_test/boom")
    def boom():
        raise RuntimeError("something nobody anticipated")

    client = app.test_client()
    res = client.get("/api/_test/boom")

    assert res.status_code == 500
    body = res.get_json()  # raises if the body isn't valid JSON (i.e. Flask's HTML error page)
    assert body["ok"] is False
    assert "error" in body


def test_a_route_returning_its_own_error_status_is_left_alone(tmp_path):
    # A route's own `return jsonify(...), 400` (or similar) is a normal
    # return value, not an exception - the error handler must never see
    # it, let alone rewrite it into a 500.
    app = _minimal_app(tmp_path)

    @app.route("/api/_test/expected-400")
    def expected_400():
        from flask import jsonify
        return jsonify(ok=False, error="bad input"), 400

    client = app.test_client()
    res = client.get("/api/_test/expected-400")

    assert res.status_code == 400
    assert res.get_json() == {"ok": False, "error": "bad input"}
