"""The window and the tray icon on Linux (KDE and others), with Qt WebEngine.
The page is the Windows app's own (desktop/web), served on this PC by
core.PageServer; it tells this program what to say in the tray through a
web channel, as it tells the Windows program through window.chrome.webview."""
import argparse
import datetime
import json
import os
import secrets
import sys
from pathlib import Path

# Hidden in the tray, the page still has to read the board on time for its
# notices: the same switches as the Windows app gives WebView2.
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                      "--disable-background-timer-throttling --disable-renderer-backgrounding "
                      "--disable-backgrounding-occluded-windows")

from PyQt6.QtCore import QFile, QIODevice, QObject, QTimer, QUrl, Qt, pyqtSlot   # noqa: E402
from PyQt6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QPainter, QPainterPath, QPixmap   # noqa: E402
from PyQt6.QtNetwork import QLocalServer, QLocalSocket, QNetworkAccessManager, QNetworkRequest   # noqa: E402
from PyQt6.QtWebChannel import QWebChannel   # noqa: E402
from PyQt6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile, QWebEngineScript,   # noqa: E402
                                   QWebEngineUrlRequestInterceptor)
from PyQt6.QtWebEngineWidgets import QWebEngineView   # noqa: E402
from PyQt6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QLabel, QLineEdit, QMainWindow,   # noqa: E402
                             QMenu, QMessageBox, QSystemTrayIcon, QVBoxLayout)

from . import __version__, core   # noqa: E402
from .core import TEXTS   # noqa: E402

HERE = Path(__file__).resolve().parent
# Installed, the page sits beside the package; in the repository, in desktop/web.
WEB_DIR = next(p for p in (HERE.parent / "web", HERE.parent.parent / "web") if (p / "index.html").exists())
ICON_FILE = next((p for p in (HERE.parent / "icon.png", HERE.parent / "localhome-desktop.png") if p.exists()), None)
INSTANCE = f"localhome-desktop-{os.getuid()}"
QUIET_MINUTES = 4          # a page silent this long is loaded again
COLOURS = {"ok": QColor(46, 157, 98), "warn": QColor(214, 134, 12), "busy": QColor(217, 115, 26), "off": QColor(118, 121, 130)}

# The page speaks to window.chrome.webview, as in WebView2: here the same
# words go to this program over the web channel; those said before it is
# ready wait for it.
BRIDGE_JS = """
(function () {
  const waiting = [];
  let host = null;
  window.chrome = window.chrome || {};
  window.chrome.webview = { postMessage: m => { const text = JSON.stringify(m); if (host) host.post(text); else waiting.push(text); } };
  new QWebChannel(qt.webChannelTransport, channel => {
    host = channel.objects.host;
    waiting.splice(0).forEach(text => host.post(text));
  });
})();
"""


class Log:
    def __init__(self, folder: Path):
        self.file = folder / "app.log"
        folder.mkdir(parents=True, exist_ok=True)

    def __call__(self, text: str) -> None:
        try:
            if self.file.exists() and self.file.stat().st_size > 1024 * 1024:
                self.file.unlink()
            with self.file.open("a", encoding="utf-8") as f:
                f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
        except OSError:
            pass    # a log is never worth an error


class Token(QWebEngineUrlRequestInterceptor):
    """Adds this run's token to the window's requests to its own server."""

    def __init__(self, port: int, token: str):
        super().__init__()
        self.port = port
        self.token = token.encode()

    def interceptRequest(self, info):
        url = info.requestUrl()
        if url.host() == "127.0.0.1" and url.port() == self.port:
            info.setHttpHeader(core.TOKEN_HEADER.encode(), self.token)


class Page(QWebEnginePage):
    """Only its own page: no other address, no new window."""

    def __init__(self, profile, port: int, parent):
        super().__init__(profile, parent)
        self.port = port

    def acceptNavigationRequest(self, url, kind, main_frame):
        return url.host() == "127.0.0.1" and url.port() == self.port

    def createWindow(self, kind):
        return None


class Bridge(QObject):
    def __init__(self, window):
        super().__init__()
        self.window = window

    @pyqtSlot(str)
    def post(self, text):
        try:
            message = json.loads(text)
        except ValueError:
            return
        if isinstance(message, dict):
            self.window.on_message(message)


class BoardDialog(QDialog):
    """The board's name and address, tried before they are kept."""

    def __init__(self, name: str, address: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(TEXTS["board_title"])
        self.name_box = QLineEdit(name or "")
        self.name_box.setMaxLength(40)
        self.address_box = QLineEdit((address or "").replace("http://", ""))
        self.note = QLabel("")
        self.note.setStyleSheet("color: #b3261e")
        self.note.setWordWrap(True)
        helptext = QLabel(TEXTS["board_help"])
        helptext.setWordWrap(True)
        helptext.setEnabled(False)
        buttons = QDialogButtonBox()
        self.save = buttons.addButton(TEXTS["board_save"], QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(TEXTS["cancel"], QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.try_it)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        for widget in (QLabel(TEXTS["board_name"]), self.name_box, QLabel(TEXTS["board_address"]), self.address_box,
                       helptext, self.note, buttons):
            layout.addWidget(widget)
        self.setMinimumWidth(420)
        (self.address_box if not address else self.name_box).setFocus()
        self.net = QNetworkAccessManager(self)
        self.board_name = self.address = None

    def try_it(self):
        address = core.normalize_address(self.address_box.text())
        if address is None:
            self.note.setText(TEXTS["address_invalid"])
            return
        self.note.setText("")
        self.save.setEnabled(False)
        request = QNetworkRequest(QUrl(address + "/api/server-time"))
        request.setTransferTimeout(6000)
        reply = self.net.get(request)
        reply.finished.connect(lambda: self.tried(reply, address))

    def tried(self, reply, address):
        answers = b"epoch_ms" in bytes(reply.readAll())
        reply.deleteLater()
        self.save.setEnabled(True)
        if not answers and QMessageBox.question(self, TEXTS["board_title"],
                                                TEXTS["address_no_answer"].format(a=address)) != QMessageBox.StandardButton.Yes:
            return
        self.board_name = self.name_box.text().strip()
        self.address = address
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self, settings: core.Settings, data_dir: Path, log: Log, start_hidden: bool):
        super().__init__()
        self.settings = settings
        self.log = log
        self.quitting = False
        self.asking = False
        self.level = ""
        self.figures = []
        self.last_status = datetime.datetime.now()
        self.base_icon = QIcon(str(ICON_FILE)) if ICON_FILE else QIcon.fromTheme("go-home")

        self.setWindowTitle(settings.shown_name())
        self.setWindowIcon(self.base_icon)
        self.setMinimumSize(320, 300)
        if not (settings.geometry and self.restoreGeometry(bytes.fromhex(settings.geometry))):
            self.resize(390, 790)
        if settings.top_most:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        token = secrets.token_urlsafe(24)
        self.server = core.PageServer(WEB_DIR, lambda: self.settings.address, token, log)
        # The view first: Qt closes a window's parts in the order they were
        # made, and the page must go before its profile.
        self.view = QWebEngineView(self)
        self.profile = QWebEngineProfile("localhome-desktop", self)
        self.profile.setPersistentStoragePath(str(data_dir / "webengine"))
        self.profile.setCachePath(str(data_dir / "webengine-cache"))
        self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.NoCache)
        self.token = Token(self.server.port, token)
        self.profile.setUrlRequestInterceptor(self.token)
        self.add_bridge_script()
        self.page = Page(self.profile, self.server.port, self.view)
        self.channel = QWebChannel(self.page)
        self.bridge = Bridge(self)
        self.channel.registerObject("host", self.bridge)
        self.page.setWebChannel(self.channel)
        self.page.renderProcessTerminated.connect(lambda *a: (self.log(f"page process ended: {a[0]}"), self.load_page()))
        self.view.setPage(self.page)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.setCentralWidget(self.view)

        self.tray = QSystemTrayIcon(self.base_icon, self)
        self.build_tray()
        self.set_level("off", settings.shown_name())
        self.tray.show()

        self.watchdog = QTimer(self)
        self.watchdog.timeout.connect(self.watch)
        self.watchdog.start(60000)
        self.log(f"start {__version__}, board {settings.address}")
        self.load_page()
        if not start_hidden:
            self.show_window()

    def add_bridge_script(self):
        source = QFile(":/qtwebchannel/qwebchannel.js")
        source.open(QIODevice.OpenModeFlag.ReadOnly)
        script = QWebEngineScript()
        script.setName("localhome-bridge")
        script.setSourceCode(bytes(source.readAll()).decode("utf-8") + BRIDGE_JS)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        self.profile.scripts().insert(script)

    # ------------------------------------------------------------ the page --

    def load_page(self):
        self.last_status = datetime.datetime.now()
        query = QUrl.toPercentEncoding
        url = (f"index.html?lang={core.LANGUAGE}&board={bytes(query(self.settings.address)).decode()}"
               f"&name={bytes(query(self.settings.shown_name())).decode()}")
        self.page.load(QUrl(self.server.url(url)))

    def on_message(self, message: dict):
        def field(name):
            value = message.get(name)
            return value if isinstance(value, str) else ""
        kind = field("type")
        if kind == "status":
            self.last_status = datetime.datetime.now()
            figures = message.get("figures")
            self.figures = [f for f in figures if isinstance(f, dict) and isinstance(f.get("value"), (int, float))] \
                if isinstance(figures, list) else []
            self.set_level(field("level"), field("tip"))
        elif kind == "notify":
            self.notify(field("kind"), field("title"), field("text"), field("level"))
        elif kind == "open":
            self.open_in_browser(field("path"))
        elif kind == "settings":
            # After this message: a dialog held open inside it would hold the page.
            QTimer.singleShot(0, self.change_board)
        elif kind == "size":
            self.set_wide(message.get("wide") is True)

    def open_in_browser(self, path: str):
        if not path.startswith("/"):
            path = "/"
        QDesktopServices.openUrl(QUrl(self.settings.address + path))

    # The page asks for room for its charts, or for the compact window again.
    def set_wide(self, wide: bool):
        if self.isMaximized():
            self.showNormal()
        self.resize(900 if wide else 390, self.height())
        self.remember_geometry()

    def watch(self):
        if (datetime.datetime.now() - self.last_status).total_seconds() >= QUIET_MINUTES * 60:
            self.log("the page went quiet: loading it again")
            self.load_page()

    # ------------------------------------------------------------ notices --

    def notify(self, kind, title, text, tone):
        if (kind == "zone" and not self.settings.notify_zones) or not title:
            return
        self.log(f"notice [{kind}]: {title} | {text.replace(chr(10), ' / ')}")
        icon = {"warn": QSystemTrayIcon.MessageIcon.Warning, "off": QSystemTrayIcon.MessageIcon.Critical}.get(
            tone, QSystemTrayIcon.MessageIcon.Information)
        self.tray.showMessage(title[:63], text[:255] or " ", icon, 10000)

    def set_level(self, level, tip):
        self.tray.setToolTip((tip or self.settings.shown_name())[:127])
        if level != self.level:
            self.log(f"state: {level} ({tip})")
        self.level = level
        self.draw_tray_icon()

    # The figure chosen for the icon: none when the board is out of reach, as
    # an old value is not the value now.
    def shown_figure(self):
        if self.level == "off" or self.settings.tray_figure == "":
            return None
        if self.settings.tray_figure in ("auto", None):
            return self.figures[0] if self.figures else None
        return next((f for f in self.figures if f.get("key") == self.settings.tray_figure), None)

    # The icon tells the state by its colour: green all fine, orange a zone
    # on, amber something to check, grey the board out of reach. With a figure
    # chosen, it shows that temperature in a tile of the colour.
    def draw_tray_icon(self):
        size = 64
        colour = COLOURS.get(self.level, COLOURS["off"])
        figure = self.shown_figure()
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if figure is None:
            self.base_icon.paint(painter, 0, 0, size, size)
            d = int(size * 0.56)
            painter.setBrush(colour)
            painter.setPen(QColor("white"))
            painter.drawEllipse(size - d, size - d, d - 2, d - 2)
        else:
            text = str(int(round(figure["value"])))
            path = QPainterPath()
            path.addRoundedRect(1, 1, size - 2, size - 2, size * 0.22, size * 0.22)
            painter.fillPath(path, colour)
            font = QFont()
            font.setBold(True)
            font.setPixelSize(int(size * (0.62 if len(text) <= 2 else 0.46)))
            painter.setFont(font)
            painter.setPen(QColor("white"))
            painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, text)
        painter.end()
        self.tray.setIcon(QIcon(pixmap))

    # --------------------------------------------------------------- tray --

    def build_tray(self):
        menu = QMenu(self)
        self.show_action = menu.addAction(TEXTS["show"], self.toggle_window)
        menu.addAction(TEXTS["open_full"], lambda: self.open_in_browser("/"))
        menu.addSeparator()
        self.figure_menu = menu.addMenu(TEXTS["in_icon"])
        self.figure_menu.aboutToShow.connect(self.fill_figure_menu)
        menu.addAction(self.check_action(TEXTS["always_on_top"], self.settings.top_most, self.set_top_most))
        menu.addAction(self.check_action(TEXTS["notify_zones"], self.settings.notify_zones, self.set_notify_zones))
        menu.addAction(self.check_action(TEXTS["start_with_session"], core.starts_with_session(), core.set_start_with_session))
        menu.addAction(TEXTS["board_settings"], self.change_board)
        menu.addSeparator()
        menu.addAction(TEXTS["exit"], self.quit)
        menu.aboutToShow.connect(lambda: self.show_action.setText(TEXTS["hide"] if self.isVisible() else TEXTS["show"]))
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_tray)
        self.tray.messageClicked.connect(self.show_window)

    def check_action(self, text, checked, change):
        action = QAction(text, self)
        action.setCheckable(True)
        action.setChecked(checked)
        action.toggled.connect(change)
        return action

    def set_top_most(self, on):
        self.settings.top_most = on
        self.settings.save()
        shown = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        if shown:
            self.show()

    def set_notify_zones(self, on):
        self.settings.notify_zones = on
        self.settings.save()

    def fill_figure_menu(self):
        self.figure_menu.clear()
        chosen = (self.figures[0].get("key") if self.figures else None) if self.settings.tray_figure in ("auto", None) \
            else self.settings.tray_figure

        def add(key, text):
            action = self.figure_menu.addAction(text, lambda: self.choose_figure(key))
            action.setCheckable(True)
            action.setChecked(self.settings.tray_figure == "" if key == "" else key == chosen)
        add("", TEXTS["only_state"])
        decimal = "," if core.ITALIAN else "."
        for f in self.figures:
            add(f.get("key", ""), f"{f.get('label', '')}  {f['value']:.1f}°".replace(".", decimal))

    def choose_figure(self, key):
        self.settings.tray_figure = key
        self.settings.save()
        self.draw_tray_icon()

    def on_tray(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_window()

    # Asked from the window and from the tray: one at a time.
    def change_board(self):
        if self.asking:
            return
        self.asking = True
        try:
            ask = BoardDialog(self.settings.board_name, self.settings.address, self if self.isVisible() else None)
            if ask.exec() != QDialog.DialogCode.Accepted:
                return
            self.settings.board_name = ask.board_name
            self.settings.address = ask.address
            self.settings.save()
            self.setWindowTitle(self.settings.shown_name())
            self.log(f"board now {self.settings.shown_name()}, {self.settings.address}")
            self.load_page()
        finally:
            self.asking = False

    def toggle_window(self):
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.show_window()

    def show_window(self):
        self.show()
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit(self):
        self.quitting = True
        self.close()

    def closeEvent(self, event):
        self.remember_geometry()
        if not self.quitting:
            # Closed by its X, it stays in the tray: the notices are its point.
            event.ignore()
            self.hide()
            if not self.settings.told_about_tray:
                self.settings.told_about_tray = True
                self.settings.save()
                self.tray.showMessage("LocalHome", TEXTS["still_running"], QSystemTrayIcon.MessageIcon.Information, 8000)
            return
        self.tray.hide()
        self.server.close()
        event.accept()
        QApplication.instance().quit()

    def remember_geometry(self):
        if self.isVisible() and not self.isMinimized():
            self.settings.geometry = bytes(self.saveGeometry()).hex()
            self.settings.save()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="localhome-desktop", description="LocalHome on the desktop")
    parser.add_argument("--tray", action="store_true", help="start hidden in the tray, as at login")
    parser.add_argument("--address", help="the board's address, as on the first start")
    parser.add_argument("--name", help="the board's name")
    parser.add_argument("--data-dir", type=Path, help="settings and log elsewhere, for a trial")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("localhome-desktop")
    app.setApplicationDisplayName("LocalHome")
    app.setDesktopFileName("localhome-desktop")
    app.setQuitOnLastWindowClosed(False)

    # One window per user: a second start shows the first one.
    probe = QLocalSocket()
    probe.connectToServer(INSTANCE)
    if probe.waitForConnected(300):
        probe.write(b"show")
        probe.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(INSTANCE)
    listener = QLocalServer()
    listener.listen(INSTANCE)

    config = args.data_dir or core.config_dir()
    settings = core.Settings(config)
    log = Log(args.data_dir or core.state_dir())
    if args.address and core.normalize_address(args.address):
        settings.address = core.normalize_address(args.address)
    if args.name:
        settings.board_name = args.name.strip()
    if not settings.address:
        ask = BoardDialog(settings.board_name, "")
        if ask.exec() != QDialog.DialogCode.Accepted:
            return 0
        settings.board_name, settings.address = ask.board_name, ask.address
    if settings.first_start and not args.data_dir:
        core.set_start_with_session(True)     # as the Windows app: on, and off from the menu
    settings.save()

    window = MainWindow(settings, config, log, args.tray)
    listener.newConnection.connect(lambda: (listener.nextPendingConnection(), window.show_window()))
    return app.exec()
