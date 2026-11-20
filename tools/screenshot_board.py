"""Screenshots of a real board's pages, for the docs, with nothing private
in them: the home page, each section of the admin panel and, with
--desktop, the Linux desktop app - all on the board's live data.

    python tools/screenshot_board.py antonio docs/tour
    python tools/screenshot_board.py antonio docs/tour --desktop

The pages read the board through a proxy on this PC that lets only GETs
through and replaces, in every answer, what must not be published: the
Shellys' ids (they hold the device's MAC), MAC and IP addresses, the board's
name and its Wi-Fi network, the board's serial, the broker's user, the
page's name, who changed the season, and the names given with --rename
("Gabriele=Cameretta": a person's name in a device's). At the end the tool
checks every answer it served: none may still hold one of those values.

Nothing on the board changes: no command, no save; the downloads that
hold secrets (.zip, .csv) and the check that asks a device are refused.
"""
import argparse
import html
import json
import re
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REFUSED = re.compile(r"(\.zip|\.csv)$|/shelly-check")
SHELLY_ID = re.compile(r"\b(shelly[a-z0-9]*-)([0-9a-f]{12})\b", re.I)
MAC = re.compile(r"\b[0-9a-f]{2}([:-])(?:[0-9a-f]{2}\1){4}[0-9a-f]{2}\b", re.I)
IPV4 = re.compile(r"\b(?:10|127|172|192)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
IPV6 = re.compile(r"\b(?:fe80|fd[0-9a-f]{2}|20[0-9a-f]{2})(?::[0-9a-f]{0,4}){2,7}\b", re.I)
TEXT = ("json", "html", "javascript", "css", "svg", "text/plain", "xml")
TALL = 1400        # CSS pixels: a taller section is cut there
ADMIN_SECTIONS = ["overview", "season", "zones", "plant-settings", "usage", "house-record", "devices",
                  "system-status", "settings", "diagnostic"]


def get_json(base: str, path: str):
    try:
        with urllib.request.urlopen(base + path, timeout=60) as r:
            return json.load(r)
    except (urllib.error.URLError, ValueError, OSError):
        return {}


class Redactor:
    """What the board says that must not be published, and what it becomes."""

    def __init__(self, base: str, board: str, renames: dict[str, str]):
        self.exact: dict[str, str] = {}
        self.ids: dict[str, str] = {}
        self.ips: dict[str, str] = {"127.0.0.1": "127.0.0.1"}
        status = get_json(base, "/api/system-status")
        net = status.get("network") or {}
        wifi = net.get("wifi") or {}
        names = {board, board.split(".")[0], net.get("hostname"), (net.get("mdns_name") or "").removesuffix(".local")}
        for name in filter(None, names):
            self.exact[name] = "casa"
        if wifi.get("ssid"):
            self.exact[wifi["ssid"]] = "Casa-WiFi"
        serial = ((status.get("host") or {}).get("hardware") or {}).get("serial")
        if serial:
            self.exact[serial] = "0000000000"
        for row in (get_json(base, "/api/climate-admin/settings").get("settings") or []):
            if row.get("key") == "site_name" and row.get("value"):
                self.exact[row["value"]] = "Casa Rossi"
        user = get_json(base, "/api/climate-admin/broker").get("username")
        if user:
            self.exact[user] = "localhome"
        for device in (get_json(base, "/api/climate-admin/shelly-health").get("devices") or []):
            if device.get("ssid"):
                self.exact[device["ssid"]] = "Casa-WiFi"
        by = (get_json(base, "/api/climate/zones").get("mode_changed") or {}).get("by")
        if by:
            self.exact[by] = "Telefono"
        self.exact.update(renames)
        # Longest first: "antonio.local" before "antonio".
        self.order = sorted(self.exact, key=len, reverse=True)

    def secrets(self) -> list[str]:
        return [s for s in self.exact if s not in ("casa",)]

    def _id(self, m):
        real = m.group(2).lower()
        self.ids.setdefault(real, f"c0ffee{len(self.ids) + 1:06d}")
        return m.group(1) + self.ids[real]

    def _ip(self, m):
        real = m.group(0)
        if real not in self.ips:
            self.ips[real] = f"192.168.1.{100 + len(self.ips)}"
        return self.ips[real]

    def apply(self, text: str) -> str:
        text = SHELLY_ID.sub(self._id, text)
        text = MAC.sub(lambda m: m.group(1).join(["02", "00", "5e", "00", "00", "01"]), text)
        text = IPV6.sub("fd00::1", text)
        text = IPV4.sub(self._ip, text)
        for real in self.order:
            new = self.exact[real]
            pattern = re.compile(rf"(?<![\w.-]){re.escape(real)}(?![\w-])", re.I)
            text = pattern.sub(new, text)
            if html.escape(real) != real:
                text = text.replace(html.escape(real), html.escape(new))
        return text


class Proxy:
    """GET only, every text answer redacted, each one kept for the check."""

    def __init__(self, base: str, redactor: Redactor):
        self.base, self.redactor, self.served, self.refused = base, redactor, [], []
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                proxy.get(self)

            def refuse(self):
                proxy.refused.append(f"{self.command} {self.path}")
                proxy.answer(self, 403, "application/json", b'{"ok":false}')

            do_POST = do_PUT = do_DELETE = do_PATCH = refuse

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def get(self, request):
        if REFUSED.search(request.path.split("?")[0]):
            self.refused.append(f"GET {request.path}")
            return self.answer(request, 403, "application/json", b'{"ok":false}')
        upstream = urllib.request.Request(self.base + request.path, headers={"Accept-Encoding": "identity"})
        try:
            with urllib.request.urlopen(upstream, timeout=60) as r:
                status, kind, data = r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            status, kind, data = e.code, e.headers.get("Content-Type", ""), e.read()
        except (urllib.error.URLError, OSError):
            return self.answer(request, 502, "application/json", b'{"ok":false,"unreachable":true}')
        if any(t in kind for t in TEXT):
            text = self.redactor.apply(data.decode("utf-8", "replace"))
            self.served.append((request.path, text))
            data = text.encode("utf-8")
        self.answer(request, status, kind, data)

    @staticmethod
    def answer(request, status, kind, data):
        try:
            request.send_response(status)
            request.send_header("Content-Type", kind or "application/octet-stream")
            request.send_header("Content-Length", str(len(data)))
            request.send_header("Cache-Control", "no-store")
            request.end_headers()
            request.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def leaks(self) -> list[str]:
        found = []
        for path, text in self.served:
            low = text.lower()
            for secret in self.redactor.secrets():
                if secret.lower() in low:
                    found.append(f"{path}: a private value")
            for m in SHELLY_ID.finditer(text):
                if not m.group(2).lower().startswith("c0ffee"):
                    found.append(f"{path}: a Shelly id")
            for m in MAC.finditer(text):
                if not m.group(0).lower().replace("-", ":").startswith("02:00:5e"):
                    found.append(f"{path}: a MAC address")
        return sorted(set(found))


def shoot(base: str, out: Path) -> list[str]:
    from playwright.sync_api import sync_playwright
    saved = []
    with sync_playwright() as p:
        browser = p.chromium.launch()

        def page_at(width, height, dark=False, scale=1):
            ctx = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=scale,
                                      color_scheme="dark" if dark else "light", locale="it-IT")
            return ctx, ctx.new_page()

        def save(page, name, element=None, full=False):
            """A section at most TALL pixels high: its first part tells what it is."""
            page.wait_for_timeout(700)
            path = out / f"{name}.png"
            if element is not None:
                box = element.bounding_box()
                if box["height"] > TALL:
                    page.screenshot(path=str(path), full_page=True,
                                    clip={"x": box["x"], "y": box["y"] + page.evaluate("scrollY"),
                                          "width": box["width"], "height": TALL})
                else:
                    element.screenshot(path=str(path))
            else:
                page.screenshot(path=str(path), full_page=full)
            if full:
                from PIL import Image
                image = Image.open(path)
                if image.height > TALL * 2:
                    image.crop((0, 0, image.width, TALL * 2)).save(path)
            saved.append(path.name)

        for name, width, height, dark, scale in (("home-phone", 390, 844, False, 2), ("home-phone-dark", 390, 844, True, 2),
                                                 ("home-desktop", 1280, 860, False, 1)):
            ctx, page = page_at(width, height, dark, scale)
            page.goto(base + "/")
            page.wait_for_selector(".zone-main", timeout=30000)
            page.wait_for_timeout(2500)
            save(page, name)
            if name == "home-desktop":
                # A zone's details: its table of the week, its hours, its lows and highs.
                page.evaluate("document.querySelector('details.zone-more').open = true")
                page.wait_for_timeout(2500)
                save(page, "home-zone-details", full=True)
            ctx.close()

        ctx, page = page_at(1100, 900)
        page.goto(base + "/climate-admin")
        page.wait_for_selector("#overview", timeout=30000)
        page.wait_for_timeout(4000)                     # the overview fills from the sections' polls
        save(page, "admin-overview")
        for section in ADMIN_SECTIONS[1:]:
            el = page.locator(f"#{section}")
            if not el.count() or not el.is_visible():
                continue
            page.evaluate(f"document.getElementById('{section}').open = true")
            el.scroll_into_view_if_needed()
            page.wait_for_timeout(2200)
            save(page, f"admin-{section}", element=el)
            page.evaluate(f"document.getElementById('{section}').open = false")
        ctx.close()
        browser.close()
    return saved


DESKTOP_SHOT = r"""
import sys, shutil
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication
from localhome_desktop import app as lh, core
out, address, data = Path(sys.argv[2]), core.normalize_address(sys.argv[3]), Path(sys.argv[4])
qapp = QApplication(sys.argv[:1])
settings = core.Settings(data)
settings.address, settings.board_name = address, "Casa Rossi"
window = lh.MainWindow(settings, data, lh.Log(data), False)
window.resize(400, 860)
shot = lambda name: window.grab().save(str(out / f"{name}.png"))
js = window.page.runJavaScript
QTimer.singleShot(10000, lambda: shot("desktop-linux-house"))
QTimer.singleShot(10500, lambda: window.resize(1180, 860))     # wide: the charts come beside the house
QTimer.singleShot(16000, lambda: shot("desktop-linux-charts"))
QTimer.singleShot(16500, lambda: js("setTab('plant')"))
QTimer.singleShot(22000, lambda: shot("desktop-linux-plant"))
QTimer.singleShot(22500, window.quit)
qapp.exec()
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("board", help="the board's name or address (antonio, 192.168.1.28:5000)")
    parser.add_argument("out", type=Path)
    parser.add_argument("--rename", action="append", default=[], metavar="REAL=SHOWN",
                        help="a name to replace, e.g. Gabriele=Cameretta")
    parser.add_argument("--desktop", action="store_true", help="also the Linux desktop app (needs PyQt6 WebEngine)")
    args = parser.parse_args()
    address = args.board if ":" in args.board else f"{args.board}:5000"
    host = address.split(":")[0]
    if "." not in host and not host.replace(".", "").isdigit():
        address = address.replace(host, f"{host}.local", 1)
    base = f"http://{address}"
    renames = dict(r.split("=", 1) for r in args.rename)
    redactor = Redactor(base, args.board, renames)
    proxy = Proxy(base, redactor)
    local = f"http://127.0.0.1:{proxy.port}"
    args.out.mkdir(parents=True, exist_ok=True)
    print(f"{base} through {local}: {len(redactor.exact)} values to replace, plus ids, MACs and IPs")
    saved = shoot(local, args.out)
    if args.desktop:
        with tempfile.TemporaryDirectory() as data:
            subprocess.run([sys.executable, "-c", DESKTOP_SHOT, str(ROOT / "desktop" / "linux"), str(args.out),
                            f"127.0.0.1:{proxy.port}", data], timeout=120)
        saved += [p.name for p in args.out.glob("desktop-linux-*.png")]
    leaks = proxy.leaks()
    print(f"saved {len(saved)}: " + ", ".join(sorted(saved)))
    print(f"refused (nothing but GETs leaves): {len(proxy.refused)}")
    if leaks:
        print("NOT CLEAN - remove the pictures:\n  " + "\n  ".join(leaks))
        return 1
    print(f"clean: {len(proxy.served)} answers checked, no private value in any")
    return 0


if __name__ == "__main__":
    sys.exit(main())
