"""How the pages travel (web/transfer.py): versioned static files kept a
year by the browser, the pages never; text gzipped for a browser that
takes it - and still answered with a 304 when nothing changed."""
import gzip
import re
import shutil
from pathlib import Path

import pytest

from localhome.web.app import create_app

ROOT = Path(__file__).resolve().parents[1]
GZIP = {"Accept-Encoding": "gzip, deflate, br"}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    home = tmp_path_factory.mktemp("transfer")
    shutil.copy(ROOT / "config" / "config.climate.example.yaml", home / "config.yaml")
    app = create_app(str(home / "config.yaml"))
    yield app.test_client(), app.config["ASSET_VERSION"]
    app.config["HISTORY"].close()


def script_url(client):
    page = client.get("/").get_data(as_text=True)
    return re.search(r'src="(/static/home/core\.js\?v=[^"]+)"', page).group(1)


def test_a_versioned_static_file_is_kept_a_year_the_pages_never(client):
    c, version = client
    url = script_url(c)
    assert url.endswith(f"v={version}")
    assert c.get(url).headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert "immutable" not in (c.get("/static/home/core.js?v=old").headers.get("Cache-Control") or ""), "another version"
    assert "immutable" not in (c.get("/static/guide/shelly-plus1-valvole.html").headers.get("Cache-Control") or "")
    assert c.get("/").headers["Cache-Control"] == "no-cache"
    assert c.get("/climate-admin").headers["Cache-Control"] == "no-cache"


def test_text_goes_gzipped_to_a_browser_that_takes_it(client):
    c, _ = client
    plain = c.get("/")
    packed = c.get("/", headers=GZIP)
    assert "Content-Encoding" not in plain.headers and "Accept-Encoding" in plain.headers["Vary"]
    assert packed.headers["Content-Encoding"] == "gzip" and "Accept-Encoding" in packed.headers["Vary"]
    assert gzip.decompress(packed.data) == plain.data
    assert int(packed.headers["Content-Length"]) == len(packed.data) < len(plain.data) / 3
    zones = c.get("/api/climate/zones", headers=GZIP)
    assert zones.headers["Content-Encoding"] == "gzip" and gzip.decompress(zones.data).startswith(b"{")


def test_a_compressed_static_file_still_answers_304(client):
    c, _ = client
    url = script_url(c)
    first = c.get(url, headers=GZIP)
    assert first.headers["Content-Encoding"] == "gzip"
    assert gzip.decompress(first.data) == (ROOT / "src/localhome/web/static/home/core.js").read_bytes()
    etag = first.headers["ETag"]
    assert etag.startswith('W/"'), "another representation of the same file"
    again = c.get(url, headers={**GZIP, "If-None-Match": etag})
    assert again.status_code == 304
    assert c.get(url, headers=GZIP).data == first.data, "compressed once, kept"


def test_what_isnt_worth_compressing_goes_as_it_is(client):
    c, _ = client
    small = c.get("/api/devices", headers=GZIP)
    assert len(small.data) < 1024 and "Content-Encoding" not in small.headers
    assert "Content-Encoding" not in c.head("/", headers=GZIP).headers
    image = next((ROOT / "src/localhome/web/static/guide").glob("*.jpg"))
    assert "Content-Encoding" not in c.get(f"/static/guide/{image.name}", headers=GZIP).headers
    assert "Content-Encoding" not in c.get("/static/home/core.js", headers={"Accept-Encoding": "br"}).headers
