"""The Linux desktop app's part without Qt (desktop/linux/localhome_desktop/core.py):
the same request lists as the Windows app, the address as the Windows app
reads it, and the local server that serves the page and passes its /api/
requests on - for this run's token only."""
import json
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop" / "linux"))
from localhome_desktop import core  # noqa: E402

TOKEN = "test-token"


def test_the_lists_are_the_windows_apps():
    source = (ROOT / "desktop" / "MainForm.cs").read_text(encoding="utf-8")
    reads, commands = re.findall(r'new Regex\(\s*@"([^"]+)"\)', source)
    assert core.READS.pattern == reads
    assert core.COMMANDS.pattern == commands


@pytest.mark.parametrize("text, address", [
    ("192.168.1.20:5000", "http://192.168.1.20:5000"),
    ("192.168.1.20", "http://192.168.1.20:5000"),
    ("board.local", "http://board.local:5000"),
    ("http://192.168.1.20:5000/", "http://192.168.1.20:5000"),
    ("https://casa.example", "https://casa.example:443"),
    ("", None),
    ("ftp://board", None),
    ("http://", None),
])
def test_an_address_as_the_windows_app_reads_it(text, address):
    assert core.normalize_address(text) == address


@pytest.fixture
def board():
    """A board that answers every path with what it was asked."""
    asked = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode() if length else None
            asked.append((method, self.path, body))
            data = json.dumps({"ok": True, "path": self.path}).encode()
            self.send_response(404 if self.path.startswith("/api/climate-admin/maintenance") else 200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.reply("GET")

        def do_POST(self):
            self.reply("POST")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", asked
    httpd.shutdown()


@pytest.fixture
def server(board, tmp_path):
    address, _ = board
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<p>page</p>", encoding="utf-8")
    (web / "app.js").write_text("// js", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("not for the page", encoding="utf-8")
    said = []
    s = core.PageServer(web, lambda: address, TOKEN, said.append)
    s.said = said
    yield s
    s.close()


def ask(server, path, method="GET", body=None, token=TOKEN):
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", data=body, method=method)
    if token:
        request.add_header(core.TOKEN_HEADER, token)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_the_page_and_its_files_only_with_the_token(server):
    assert ask(server, "/index.html?lang=it") == (200, b"<p>page</p>")
    assert ask(server, "/app.js")[0] == 200
    assert ask(server, "/index.html", token=None)[0] == 403, "another program on this PC gets nothing"
    assert ask(server, "/index.html", token="guess")[0] == 403
    assert ask(server, "/../secret.txt")[0] == 404
    assert ask(server, "/%2e%2e/secret.txt")[0] == 404


def test_a_listed_read_and_a_listed_command_reach_the_board(server, board):
    _, asked = board
    status, data = ask(server, "/api/sensors/Stanze%20Sensor/history?field=temperature_c&range=24h")
    assert status == 200 and json.loads(data)["ok"] is True
    status, _ = ask(server, "/api/climate/away", "POST", b'{"hours": 24}')
    assert status == 200
    assert asked == [("GET", "/api/sensors/Stanze%20Sensor/history?field=temperature_c&range=24h", None),
                     ("POST", "/api/climate/away", '{"hours": 24}')]
    assert server.said == ["command: /api/climate/away -> 200"]


def test_what_the_page_may_not_ask_stays_here(server, board):
    _, asked = board
    for method, path in (("GET", "/api/climate-admin/diagnostics.zip"), ("POST", "/api/sensors/VMC/off"),
                         ("GET", "/api/climate-admin/shelly-check"), ("POST", "/api/climate/mode")):
        assert ask(server, path, method, b"{}" if method == "POST" else None) == (403, b'{"ok":false}')
    assert asked == []


def test_the_boards_own_answer_and_status_come_back(server):
    status, data = ask(server, "/api/climate-admin/maintenance")
    assert status == 404, "an older board's 404 reaches the page as it is"


def test_a_board_out_of_reach_is_said_as_such(server):
    server.address = lambda: "http://127.0.0.1:9"
    assert ask(server, "/api/problems") == (502, b'{"ok":false,"unreachable":true}')


def test_the_settings_come_back_as_saved(tmp_path):
    settings = core.Settings(tmp_path)
    assert settings.first_start and settings.shown_name() == "LocalHome"
    settings.board_name, settings.address, settings.tray_figure = "Casa", "http://board.local:5000", "outdoor"
    settings.save()
    again = core.Settings(tmp_path)
    assert not again.first_start
    assert (again.shown_name(), again.address, again.tray_figure, again.notify_zones) == ("Casa", "http://board.local:5000", "outdoor", True)
    (tmp_path / "settings.json").write_text("{broken", encoding="utf-8")
    assert core.Settings(tmp_path).address == "", "a damaged file: as on a first start"


def test_start_at_login_writes_and_removes_its_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    core.set_start_with_session(True)
    assert core.starts_with_session()
    assert "Exec=localhome-desktop --tray" in core.autostart_file().read_text(encoding="utf-8")
    core.set_start_with_session(False)
    assert not core.starts_with_session()
