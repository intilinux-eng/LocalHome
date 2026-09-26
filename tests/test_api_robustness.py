"""No route answers a malformed request with a 500.

Found by fuzzing every route: a body that parses as JSON but isn't an
object (a bare number, a string) reached `.get()` in every POST route,
and an absurd away duration overflowed the date. A 4xx is the right
answer to both; this walks every route so a new one can't regress.
"""
import pytest

from localhome.web.app import create_app

CONFIG = """
web:
  port: 5000
integrations:
  - kind: climate
    type: simulated
    name: "Rooms Sensor"
  - kind: switch
    type: simulated
    name: "Rooms Valve"
thermostat:
  zones:
    - name: "Rooms"
      sensor: "Rooms Sensor"
      valve: "Rooms Valve"
"""
BODIES = ["123", '"text"', "[]", "null", "not json", '{"hours": 1e308}', '{"hours": true}']


@pytest.fixture
def app(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    app = create_app(str(path))
    yield app
    app.config["HISTORY"].close()


def test_no_post_route_answers_a_malformed_body_with_a_500(app):
    client = app.test_client()
    failures = []
    for rule in app.url_map.iter_rules():
        if "POST" not in rule.methods or "restart" in rule.rule:
            continue
        url = rule.rule
        for arg in rule.arguments:
            url = url.replace(f"<{arg}>", "Rooms")
        for body in BODIES:
            res = client.post(url, data=body, content_type="application/json")
            if res.status_code >= 500:
                failures.append((url, body, res.status_code))
    assert failures == []


def test_an_absurd_away_duration_is_refused(app):
    res = app.test_client().post("/api/climate/away", json={"hours": 10**9})
    assert res.status_code == 400 and "between" in res.get_json()["error"]
    assert app.test_client().post("/api/climate/away", json={"hours": 24}).status_code == 200
