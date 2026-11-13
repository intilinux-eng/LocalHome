"""What the Linux app does without Qt: its settings, its words, and the small
local server that gives the window its page and passes the page's /api/
requests on to the board.

The server listens on this PC only (127.0.0.1) and answers only requests
that carry this run's token: the window adds it to each request (app.py), a
browser or another program on this PC can't. It passes on only the requests
the page makes, by the same lists as the Windows app (desktop/MainForm.cs),
so the window can't reach anything else on the board."""
import json
import locale
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_PORT = 5000
TOKEN_HEADER = "X-LocalHome-Token"

# The same lists as desktop/MainForm.cs: a test keeps them equal.
READS = re.compile(
    r"^/api/(climate/(zones|run-hours|temps|outdoor)|problems|devices|readings|vmc-schedule|covers|server-time|system-status|dehumidifier/settings|sensors/[^/]+/history|energy/(?!import$)[^/]+|climate-admin/(shelly-health(/revision)?|broker(-status)?|map|extra-(switches|sensors|dimmers)|settings|vmc-(speeds|auto)|led-matrix|maintenance|dataset))$")
COMMANDS = re.compile(
    r"^/api/(vmc-schedule/boost(/cancel)?|covers/[^/]+/action/(open|stop|close)|sensors/[^/]+/(set-value|on)|climate/zones/[^/]+/override(/cancel)?|climate/away(/cancel)?|dehumidifier/(on|off))$")
FILE_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}


def allowed(method: str, path: str) -> bool:
    """A request the page may make: a listed read, or a listed command."""
    if method == "GET":
        return bool(READS.match(path))
    return method == "POST" and bool(COMMANDS.match(path))


def normalize_address(text: str | None) -> str | None:
    """"192.168.1.20:5000", "board.local", "http://192.168.1.20:5000/": the
    base address, or None when it can't be one. With no port, the board's
    usual one (as desktop/Settings.cs)."""
    text = (text or "").strip()
    had_scheme = "://" in text
    text = text.rstrip("/")
    if not text:
        return None
    if not had_scheme:
        text = "http://" + text
    try:
        url = urllib.parse.urlsplit(text)
        port = url.port
    except ValueError:
        return None
    if url.scheme not in ("http", "https") or not url.hostname:
        return None
    if port is None:
        port = 443 if had_scheme and url.scheme == "https" else DEFAULT_PORT
    host = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
    return f"{url.scheme}://{host}:{port}"


# ----------------------------------------------------------------- places --

def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "localhome-desktop"


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "localhome-desktop"


def autostart_file() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "localhome-desktop.desktop"


AUTOSTART = """[Desktop Entry]
Type=Application
Name=LocalHome
Exec=localhome-desktop --tray
Icon=localhome-desktop
X-GNOME-Autostart-enabled=true
"""


def starts_with_session() -> bool:
    return autostart_file().exists()


def set_start_with_session(on: bool) -> None:
    file = autostart_file()
    if on:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(AUTOSTART, encoding="utf-8")
    elif file.exists():
        file.unlink()


# --------------------------------------------------------------- settings --

class Settings:
    """What this PC remembers, in the user's own folder: the board's name and
    address, the window's choices. Nothing about this app lives on the board."""

    FIELDS = {"board_name": "", "address": "", "top_most": False, "notify_zones": True,
              "told_about_tray": False, "tray_figure": "auto", "geometry": ""}

    def __init__(self, folder: Path):
        self.file = folder / "settings.json"
        self.first_start = not self.file.exists()
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        for key, default in self.FIELDS.items():
            value = data.get(key, default) if isinstance(data, dict) else default
            setattr(self, key, value if isinstance(value, type(default)) else default)

    def shown_name(self) -> str:
        return self.board_name.strip() or "LocalHome"

    def save(self) -> None:
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps({k: getattr(self, k) for k in self.FIELDS}, indent=1), encoding="utf-8")
            tmp.replace(self.file)
        except OSError:
            pass    # tried again at the next change


# ------------------------------------------------------------------ words --

def italian() -> bool:
    lang = os.environ.get("LANGUAGE") or os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") \
        or (locale.getlocale()[0] or "")
    return lang.lower().startswith("it")


ITALIAN = italian()
LANGUAGE = "it" if ITALIAN else "en"


def t(it: str, en: str) -> str:
    return it if ITALIAN else en


TEXTS = {
    "show": t("Mostra", "Show"),
    "hide": t("Nascondi", "Hide"),
    "always_on_top": t("Sempre in primo piano", "Always on top"),
    "notify_zones": t("Avvisa quando si accende una zona", "Tell me when a zone turns on"),
    "start_with_session": t("Avvia all'accesso", "Start at login"),
    "board_settings": t("Impostazioni della scheda…", "Board settings…"),
    "open_full": t("Apri la pagina completa nel browser", "Open the full page in the browser"),
    "in_icon": t("Nell'icona", "In the icon"),
    "only_state": t("Solo il colore dello stato", "Only the colour of the state"),
    "exit": t("Esci", "Exit"),
    "board_title": t("Impostazioni della scheda", "Board settings"),
    "board_name": t("Nome (facoltativo)", "Name (optional)"),
    "board_address": t("Indirizzo", "Address"),
    "board_help": t("IP e porta, o il nome di rete: 192.168.1.20:5000, scheda.local. Senza porta si usa la 5000.",
                    "IP and port, or its network name: 192.168.1.20:5000, board.local. With no port, 5000."),
    "board_save": t("Prova e salva", "Test and save"),
    "cancel": t("Annulla", "Cancel"),
    "address_invalid": t("Questo non sembra un indirizzo.", "That doesn't look like an address."),
    "address_no_answer": t("{a} non risponde come LocalHome. Lo salvo comunque?", "{a} doesn't answer as LocalHome. Save it anyway?"),
    "still_running": t("LocalHome resta attivo qui, per gli avvisi. Per chiuderlo: tasto destro sull'icona, Esci.",
                       "LocalHome stays on here, for its notices. To close it: right-click the icon, Exit."),
}


# ----------------------------------------------------------- the server --

class PageServer:
    """The window's page from `web_dir`, and its /api/ requests passed on to
    the board at `address()` - on 127.0.0.1, for this run's token only."""

    def __init__(self, web_dir: Path, address, token: str, log):
        self.web_dir = Path(web_dir)
        self.address = address        # a function: the board's base address now
        self.token = token
        self.log = log
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):     # the program's own log says what matters
                pass

            def do_GET(self):
                server.handle(self, "GET")

            def do_POST(self):
                server.handle(self, "POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, name="page-server", daemon=True).start()

    def url(self, page: str) -> str:
        return f"http://127.0.0.1:{self.port}/{page}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def handle(self, request, method: str) -> None:
        if request.headers.get(TOKEN_HEADER) != self.token:
            return self.answer(request, 403, "text/plain", b"")
        split = urllib.parse.urlsplit(request.path)
        path = split.path
        length = int(request.headers.get("Content-Length") or 0)
        body = request.rfile.read(length) if length else None
        if not path.startswith("/api/"):
            return self.local_file(request, method, path)
        if not allowed(method, path):
            self.log(f"refused: {method} {path}")
            return self.answer(request, 403, "application/json", b'{"ok":false}')
        status, data = self.forward(method, path + (f"?{split.query}" if split.query else ""), body)
        if method == "POST":
            self.log(f"command: {path} -> {status}")
        self.answer(request, status, "application/json", data)

    def local_file(self, request, method: str, path: str) -> None:
        name = "index.html" if path == "/" else path.lstrip("/")
        file = (self.web_dir / name).resolve()
        if method != "GET" or file.parent != self.web_dir.resolve() or file.suffix not in FILE_TYPES or not file.is_file():
            return self.answer(request, 404, "text/plain", b"Not Found")
        self.answer(request, 200, FILE_TYPES[file.suffix], file.read_bytes())

    def forward(self, method: str, path_and_query: str, body: bytes | None) -> tuple[int, bytes]:
        request = urllib.request.Request(self.address() + path_and_query, data=body, method=method)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=14) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()
        except (urllib.error.URLError, OSError, ValueError):
            return 502, b'{"ok":false,"unreachable":true}'

    @staticmethod
    def answer(request, status: int, kind: str, data: bytes) -> None:
        try:
            request.send_response(status)
            request.send_header("Content-Type", kind)
            request.send_header("Content-Length", str(len(data)))
            request.send_header("Cache-Control", "no-store")
            request.end_headers()
            request.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass    # the page went away meanwhile
