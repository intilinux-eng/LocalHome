"""The name on the dashboard is the installation's own, set from the
admin panel: "LocalHome" is only the default.

It has to fit a phone's header and the label under a home-screen icon, so
it's checked before it's saved - and unlike the other settings it's read
on every page render, so a new name is there on the next reload instead of
after a restart.
"""
import pytest

from localhome.services import panel_settings
from localhome.web.app import create_app

CONFIG = """
web:
  port: 5000
  language: "en"
integrations: []
"""


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    app = create_app(str(path))
    yield app.test_client()
    app.config["HISTORY"].close()


def _save(client, name):
    return client.post("/api/climate-admin/settings", json={"values": {"site_name": name}})


# ------------------------------------------------------------ checks ----

def test_a_name_that_would_not_fit_is_refused_saying_by_how_much():
    too_long = "Casa di Antonio e famiglia, piano primo"
    with pytest.raises(ValueError) as exc:
        panel_settings.validate({"site_name": too_long})
    assert "24 characters" in str(exc.value) and str(len(too_long)) in str(exc.value)


def test_a_name_of_exactly_the_limit_is_fine():
    name = "x" * panel_settings.SITE_NAME_MAX
    assert panel_settings.validate({"site_name": name}) == {"site_name": name}


@pytest.mark.parametrize("bad", ["Casa\nmia", "Casa\tmia\x07", "Casa\x00"])
def test_control_characters_are_refused_or_flattened(bad):
    # A tab or a newline is just a space typed oddly; a real control
    # character is a paste accident and says so.
    try:
        clean = panel_settings.validate({"site_name": bad})["site_name"]
    except ValueError as exc:
        assert "control characters" in str(exc)
    else:
        assert clean == "Casa mia"


@pytest.mark.parametrize("bad", [["Casa"], {"a": 1}, True])
def test_something_that_is_not_text_is_refused(bad):
    with pytest.raises(ValueError, match="must be text"):
        panel_settings.validate({"site_name": bad})


def test_stray_spaces_are_tidied_up():
    assert panel_settings.validate({"site_name": "  Casa   mia  "}) == {"site_name": "Casa mia"}


def test_blank_means_the_default_name():
    assert panel_settings.validate({"site_name": "   "}) == {}


# -------------------------------------------------------------- page ----

def test_the_default_name_is_localhome(client):
    page = client.get("/").get_data(as_text=True)
    assert '<span class="brand-name">LocalHome</span>' in page


def test_a_saved_name_is_on_the_page_without_a_restart(client):
    res = _save(client, "Casa Rossi")

    assert res.status_code == 200
    assert res.get_json()["restart_required"] is False, "the name is live, don't ask for a restart"
    page = client.get("/").get_data(as_text=True)
    assert '<span class="brand-name">Casa Rossi</span>' in page
    assert "<title>Casa Rossi</title>" in page
    assert '<meta name="apple-mobile-web-app-title" content="Casa Rossi">' in page
    assert "Casa Rossi" in client.get("/climate-admin").get_data(as_text=True)


def test_the_name_on_a_phone_home_screen_follows_too(client):
    _save(client, "Casa Rossi")

    manifest = client.get("/site.webmanifest").get_json()
    assert manifest["name"] == "Casa Rossi"
    assert manifest["short_name"] == "Casa Rossi"
    assert [icon["src"] for icon in manifest["icons"]] == [
        "/static/icon-192.png", "/static/icon-512.png", "/static/icon.svg"]


def test_clearing_it_brings_localhome_back(client):
    _save(client, "Casa Rossi")

    _save(client, "")

    assert '<span class="brand-name">LocalHome</span>' in client.get("/").get_data(as_text=True)
    assert client.get("/site.webmanifest").get_json()["name"] == "LocalHome"


def test_a_name_that_is_too_long_is_refused_and_nothing_changes(client, tmp_path):
    res = _save(client, "Casa di Antonio e famiglia, piano primo")

    assert res.status_code == 400
    assert "24" in res.get_json()["error"]
    assert not (tmp_path / "panel_settings.json").exists()
    assert '<span class="brand-name">LocalHome</span>' in client.get("/").get_data(as_text=True)


def test_a_name_cannot_smuggle_markup_into_the_page(client):
    _save(client, "<script>x</script>")

    page = client.get("/").get_data(as_text=True)
    assert "<script>x</script>" not in page
    assert "&lt;script&gt;" in page


def test_the_name_field_is_offered_with_its_limit(client):
    row = next(r for r in client.get("/api/climate-admin/settings").get_json()["settings"]
               if r["key"] == "site_name")

    assert (row["type"], row["max_length"], row["default"]) == ("text", 24, "LocalHome")
    assert (row["value"], row["source"]) == ("LocalHome", "default")


def test_a_name_in_config_yaml_is_shown_as_such(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG.replace('  language: "en"', '  language: "en"\n  site_name: "Casa di prova"'),
                    encoding="utf-8")
    app = create_app(str(path))
    try:
        assert '<span class="brand-name">Casa di prova</span>' in app.test_client().get("/").get_data(as_text=True)
        row = next(r for r in app.test_client().get("/api/climate-admin/settings").get_json()["settings"]
                   if r["key"] == "site_name")
        assert (row["value"], row["source"]) == ("Casa di prova", "config")
    finally:
        app.config["HISTORY"].close()


def test_the_name_does_not_make_the_panel_ask_for_a_restart(client):
    _save(client, "Casa Rossi")

    settings = client.get("/api/climate-admin/settings").get_json()
    assert settings["restart_pending"] is False

    # A setting that really does need one still says so.
    client.post("/api/climate-admin/settings", json={
        "values": {"site_name": "Casa Rossi", "history_retention_days": 14}})
    assert client.get("/api/climate-admin/settings").get_json()["restart_pending"] is True


def test_the_home_page_shows_the_name_in_its_top_bar_and_title(client):
    _save(client, "Casa Rossi")
    page = client.get("/").get_data(as_text=True)
    assert '<span class="brand-name">Casa Rossi</span>' in page
    assert "<title>Casa Rossi</title>" in page
