"""The home page says where its source is - the AGPL-3.0's offer to whoever
uses it over the network: this project's repository, or the one a
modified copy names in config.yaml (web.source_url)."""
from localhome import SOURCE_URL
from localhome.web.app import create_app

CONFIG = """
web:
  port: 5000
{extra}integrations: []
"""


def _home(tmp_path, extra=""):
    (tmp_path / "config.yaml").write_text(CONFIG.format(extra=extra), encoding="utf-8")
    app = create_app(str(tmp_path / "config.yaml"))
    try:
        return app.test_client().get("/").get_data(as_text=True)
    finally:
        app.config["HISTORY"].close()


def test_the_home_page_links_to_this_projects_source(tmp_path):
    page = _home(tmp_path)
    assert f'<a href="{SOURCE_URL}" target="_blank" rel="noopener">' in page


def test_a_modified_copy_links_to_its_own_source(tmp_path):
    page = _home(tmp_path, '  source_url: "https://example.org/my-fork"\n')
    assert '<a href="https://example.org/my-fork"' in page and SOURCE_URL not in page
