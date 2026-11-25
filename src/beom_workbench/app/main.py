"""beom-workbench 데스크톱 앱 — 네이티브 창 안에 화면을 그린다 (Qt WebEngine). 브라우저도 서버도 쓰지 않는다.

화면과 툴킷은 Qt WebChannel(같은 프로세스 안의 연결)로만 통한다. 네트워크 포트를 열지 않으므로 승인·답변 같은
사람의 결정은 이 창에서만 기록되고, 에이전트가 대신 누를 경로가 없다. 바깥 주소로의 이동은 막는다.
"""
from __future__ import annotations

import os
import sys
from importlib import resources
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineSettings, QWebEngineUrlRequestJob, QWebEngineUrlScheme,
                                     QWebEngineUrlSchemeHandler)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QSystemTrayIcon, QWidget

from beom_workbench.app.bridge import Api
from beom_workbench.config import load_settings


def web_root() -> Path:
    return Path(str(resources.files("beom_workbench.app") / "web"))


# 화면 설계 프로토타입을 앱 화면과 다른 출처로 띄우는 주소 — 앱 화면의 보안 규칙(인라인 스크립트 금지)을 물려받지 않아
# 프로토타입 안의 탭·펼치기 스크립트가 돌고, 앱의 다리(WebChannel)와는 떨어져 있다. QApplication 전에 등록해야 한다.
PROTO_SCHEME = b"beomproto"


def _register_scheme() -> None:
    if QWebEngineUrlScheme.schemeByName(PROTO_SCHEME).name():
        return
    sc = QWebEngineUrlScheme(PROTO_SCHEME)
    sc.setSyntax(QWebEngineUrlScheme.Syntax.Path)
    # 앱 화면(file://)이 틀로 띄울 수 있게 로컬 주소로 — 틀은 sandbox 로 앱 화면과 떨어져 있다
    sc.setFlags(QWebEngineUrlScheme.Flag.SecureScheme | QWebEngineUrlScheme.Flag.LocalScheme)
    QWebEngineUrlScheme.registerScheme(sc)


_register_scheme()


class ProtoHandler(QWebEngineUrlSchemeHandler):
    """beomproto:/<run_id>/<파일> — 그 실행의 .beom/brief/wireframes 안 HTML 만, 보기용 작은 스크립트(proto-frame.js)를 넣어 내준다."""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings

    def requestStarted(self, job: QWebEngineUrlRequestJob) -> None:  # noqa: N802
        from urllib.parse import unquote

        from beom_workbench.core import brief, inbox

        try:
            parts = [unquote(x) for x in job.requestUrl().path().strip("/").split("/") if x]
            run = inbox.load_run(self.settings, parts[0])
            d = brief.folder(run["target_root"]) / "wireframes"
            f = d / Path(parts[-1]).name
            if f.suffix.lower() not in (".html", ".htm") or not f.is_file() or f.is_symlink() or f.stat().st_size > 3 * 1024 * 1024:
                raise FileNotFoundError(f)
            html = f.read_text(encoding="utf-8", errors="replace")
            from beom_workbench.core import wfkit
            html = wfkit.inline(html, d)  # 공통 부품(_kit.css)을 화면 안에 — 격리된 틀은 바깥 파일을 읽지 않는다
        except Exception:  # noqa: BLE001 — 없는 화면·잘못된 주소
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            return
        tag = "<script>" + (web_root() / "proto-frame.js").read_text(encoding="utf-8") + "</script>"
        low = html.lower()
        html = html[:low.rindex("</body>")] + tag + html[low.rindex("</body>"):] if "</body>" in low else html + tag
        buf = QBuffer(job)
        buf.setData(QByteArray(html.encode("utf-8")))
        buf.open(QIODevice.ReadOnly)
        job.reply(b"text/html; charset=utf-8", buf)


APP_NAME = "beom-workbench"
APP_ID = "beom.workbench.app"
BG = "#f6f6f3"
BRAND = "#a85a08"  # app.css --signal (밝은 테마) — 아이콘 '작업대'의 호박


def _icon_pixmap(size: int) -> QPixmap:
    """작업 표시줄·트레이·바로가기 아이콘 — 사이드바 로고와 같은 파일(web/mark.svg)을 그린다 (따로 그리면 비율이 어긋났다)."""
    from PySide6.QtSvg import QSvgRenderer
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    # 32px 이하(작업 표시줄·탭)는 그림자 없는 작은 판 — 큰 판의 그림자·얇은 금색이 작은 크기에서 뭉개졌다
    art = "mark-small.svg" if size <= 32 else "mark.svg"
    QSvgRenderer(QByteArray((web_root() / art).read_bytes())).render(p, QRectF(0, 0, size, size))
    p.end()
    return pm


def refresh_icon_files(home: Path) -> None:
    """바로가기가 가리키는 아이콘 파일이 있는데 지금 그림과 다르면 다시 쓴다 — 로고를 바꿔도 바로가기는 예전 그림으로 남았다."""
    ico = home / "app" / "app.ico"
    if not ico.is_file():
        return
    try:
        old = ico.read_bytes()
        new_ico = write_icon_files(home, dry=True)
        if new_ico != old:
            write_icon_files(home)
    except Exception:  # noqa: BLE001 — 아이콘을 못 써도 앱은 그대로
        pass


def write_icon_files(home: Path, dry: bool = False):
    """바로가기용 아이콘 파일 — app.ico(여러 크기 PNG 를 담은 ICO)와 app.png. 앱 창이 없어도 그린다. dry=True 면 쓰지 않고 ICO 바이트만."""
    import struct

    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QGuiApplication

    if QGuiApplication.instance() is None:
        _app = QGuiApplication([])  # noqa: F841 — 그림을 그리려면 하나 있어야 한다
    if not dry:
        home.mkdir(parents=True, exist_ok=True)
    blobs = []
    for size in (16, 24, 32, 48, 64, 128, 256):
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.WriteOnly)
        _icon_pixmap(size).save(buf, "PNG")
        blobs.append((size, bytes(ba)))
    head = struct.pack("<HHH", 0, 1, len(blobs))
    entries, data, offset = b"", b"", 6 + 16 * len(blobs)
    for size, png in blobs:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset)
        data += png
        offset += len(png)
    if dry:
        return head + entries + data
    ico = home / "app" / "app.ico"  # 바로가기가 가리키므로 지워도 되는 cache/ 가 아니라 app/
    ico.parent.mkdir(parents=True, exist_ok=True)
    ico.write_bytes(head + entries + data)
    (home / "app" / "app.png").write_bytes(blobs[-1][1])
    return ico


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        icon.addPixmap(_icon_pixmap(size))
    return icon


def _windows_setup() -> None:
    """Windows 전용 준비.

    - 작업 표시줄이 이 창을 python 이 아니라 별도 앱으로 묶고 우리 아이콘을 쓰게 앱 ID 를 정한다.
    - 이 앱은 콘솔 없는 창 프로그램이라, 콘솔 프로그램(git·codex·claude)을 띄울 때마다 Windows 가 터미널 창을
      잠깐 열었다 닫는다. 따로 창을 정하지 않은 자식 프로세스는 창 없이 띄운다 (로그인 창처럼 일부러 여는 것은 그대로).
    """
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError):
        pass
    from beom_workbench.common import nowindow

    nowindow.install()


class Page(QWebEnginePage):
    """앱 안의 화면만 보여 준다. 링크는 기본 브라우저로 넘기고, 창 안에서는 바깥 주소로 가지 않는다."""

    def __init__(self, parent=None, on_drop=None):
        super().__init__(parent)
        self.on_drop = on_drop

    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:
        if url.scheme() == "file":
            # 앱 화면 파일만 — 창에 끌어다 놓은 파일로 화면이 바뀌지 않게, 그 파일은 첨부로 넘긴다
            if url.host():  # file://서버/공유 — 파일 시스템을 건드리기 전에 거절 (네트워크 접속이 일어나지 않게)
                return False
            try:
                inside = Path(url.toLocalFile()).resolve().is_relative_to(web_root().resolve())
            except (OSError, ValueError):
                inside = False
            if inside:
                return True
            if is_main_frame and self.on_drop and Path(url.toLocalFile()).is_file():
                self.on_drop([url.toLocalFile()])
            return False
        if url.scheme() in ("qrc", "data"):
            return True
        if url.scheme() == PROTO_SCHEME.decode() and not is_main_frame:  # 화면 설계 프로토타입 (틀 안에서만)
            return True
        if url.scheme() in ("http", "https") and nav_type == QWebEnginePage.NavigationTypeLinkClicked:
            QDesktopServices.openUrl(url)
        return False


class DropFilter(QObject):
    """창에 끌어다 놓은 파일을 첨부로 — 웹 화면은 파일의 실제 경로를 모르므로 Qt 에서 받아 경로째 넘긴다."""

    def __init__(self, on_drop, parent=None):
        super().__init__(parent)
        self.on_drop = on_drop

    @staticmethod
    def _paths(e) -> list[str]:
        md = e.mimeData()
        return [u.toLocalFile() for u in md.urls() if u.isLocalFile()] if md and md.hasUrls() else []

    def eventFilter(self, obj, e) -> bool:  # noqa: N802
        t = e.type()
        if t == QEvent.ChildAdded and e.child().isWidgetType():  # 웹 화면의 실제 그리기 위젯은 나중에 생긴다
            e.child().installEventFilter(self)
        elif t in (QEvent.DragEnter, QEvent.DragMove) and self._paths(e):
            e.acceptProposedAction()
            return True
        elif t == QEvent.Drop and self._paths(e):
            e.acceptProposedAction()
            self.on_drop(self._paths(e))
            return True
        return False


class Window(QMainWindow):
    def __init__(self, settings=None):
        super().__init__()
        self.settings = settings or load_settings()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        try:  # 앱 바깥 부품(브라우저 패널의 단추·주소창)도 앱과 같은 글꼴로
            from PySide6.QtGui import QFontDatabase
            QFontDatabase.addApplicationFont(str(web_root() / "fonts" / "PretendardVariable.woff2"))
            for f in (Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "malgun.ttf",):
                if f.is_file():
                    QFontDatabase.addApplicationFont(str(f))
        except Exception:  # noqa: BLE001 — 글꼴이 없으면 시스템 글꼴
            pass
        self.resize(1400, 880)
        self.setMinimumSize(1080, 680)
        self.view = QWebEngineView(self)
        page = Page(self.view, on_drop=self._dropped)
        self.drops = DropFilter(self._dropped, self)
        self.view.installEventFilter(self.drops)
        for w in self.view.findChildren(QWidget):
            w.installEventFilter(self.drops)
        s = page.settings()
        s.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, False)
        s.setAttribute(QWebEngineSettings.JavascriptCanOpenWindows, False)
        page.setBackgroundColor(QColor(BG))
        self.view.setPage(page)
        self.proto = ProtoHandler(self.settings, self)
        prof = page.profile()
        if not prof.urlSchemeHandler(PROTO_SCHEME):
            prof.installUrlSchemeHandler(PROTO_SCHEME, self.proto)
        self.api = Api(self.settings, self)
        self.channel = QWebChannel(page)
        self.channel.registerObject("api", self.api)
        page.setWebChannel(self.channel)
        self.view.setContextMenuPolicy(Qt.NoContextMenu)
        self.view.load(QUrl.fromLocalFile(str(web_root() / "index.html")))
        # 앱 화면 | 브라우저 패널 — 다른 작업 도구처럼 대화 옆에 붙는다 (처음에는 닫혀 있다)
        from PySide6.QtWidgets import QSplitter
        self.split = QSplitter(Qt.Horizontal, self)
        self.split.addWidget(self.view)
        self.split.setChildrenCollapsible(False)
        self.view.setMinimumWidth(640)
        self.browser = None
        self.setCentralWidget(self.split)
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip(APP_NAME)
        self.tray.activated.connect(lambda *_: (self.showNormal(), self.activateWindow()))
        self.tray.messageClicked.connect(lambda: (self.showNormal(), self.activateWindow()))
        self.tray.show()
        self.api.notify.connect(self._notify)
        self.api.attention.connect(self._flash)

    def show_browser(self, url: str = "", key: str = "") -> None:
        """오른쪽 브라우저 패널 — 처음 열 때 만든다. 가져온 화면은 대화 입력창(또는 새 작업)의 첨부로 간다."""
        import json as _json

        from beom_workbench.app.linkbrowser import LinkBrowser
        if self.browser is None:
            self.browser = LinkBrowser(self.settings, "", self.split)
            self.browser.setMinimumWidth(420)
            self.browser.setMaximumWidth(720)  # 대화가 눌리지 않게 — 넓게 보려면 창을 키운다
            self.split.setStretchFactor(0, 1)
            self.split.setStretchFactor(1, 0)
            self.browser.saved.connect(lambda u, p: self.api.linkSaved.emit(_json.dumps({"url": u, "path": p}, ensure_ascii=False)))
            self.browser.closed.connect(lambda: self.api.browserToggled.emit("closed"))
            self.split.addWidget(self.browser)
        if not self.browser.isVisible():
            self.browser.show()
            from PySide6.QtCore import QTimer as _QT

            def size():  # 앱의 오른쪽 패널 자리를 브라우저가 대신한다 — 대화는 그대로 넓게 (앱 화면 780px 이상)
                w = max(self.split.width(), self.width())
                pane = max(420, min(640, w - 780))
                self.split.setSizes([w - pane, pane])
            _QT.singleShot(0, size)
            _QT.singleShot(200, size)  # 패널이 처음 자리 잡은 뒤 한 번 더 (처음 한 번은 크기 힌트에 밀렸다)
        self.browser.open(url, key)
        self.api.browserToggled.emit("open")

    def _dropped(self, paths: list[str]) -> None:
        import json
        self.api.filesDropped.emit(json.dumps(paths))

    def closeEvent(self, e) -> None:  # noqa: N802
        """닫을 때 이 창이 돌리던 작업을 멈춘다 — 에이전트·개발 서버가 감시 없이 계속 폴더를 고치지 않게."""
        import time
        busy = [rid for rid, t in list(self.api.threads.items()) if t.is_alive()]
        if busy:
            from beom_workbench import i18n
            ans = QMessageBox.question(self, APP_NAME, i18n.tr(
                f"진행 중인 작업이 {len(busy)}개 있습니다.\n닫으면 멈춥니다 — 나중에 '이어서'로 계속할 수 있습니다. 닫을까요?", self.api.lang()))
            if ans != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            for rid in busy:
                if rid in self.api.runners:
                    self.api.runners[rid].stop()
            until = time.monotonic() + 15
            for rid in busy:
                self.api.threads[rid].join(max(0.1, until - time.monotonic()))
        super().closeEvent(e)

    def _notify(self, title: str, body: str) -> None:
        """창을 보고 있으면 띄우지 않는다. 알림을 누르면 창이 앞으로 온다. 작업 표시줄도 함께 깜빡인다."""
        if self.isActiveWindow():
            return
        if QSystemTrayIcon.supportsMessages():
            self.tray.showMessage(title, body, app_icon(), 6000)
        self._flash()

    def _flash(self) -> None:
        if not self.isActiveWindow():
            QApplication.alert(self, 0)  # 창을 볼 때까지 작업 표시줄 단추가 주황으로 깜빡인다 (Windows 관례)

    def changeEvent(self, e) -> None:
        if e.type() == QEvent.ActivationChange and self.isActiveWindow():
            self.api.seen()  # 다시 봤다 — 다음 확인 요청은 다시 알린다
        if e.type() == QEvent.WindowStateChange:
            # 최소화하면 화면이 '안 보임'이 되게 — Qt 는 최소화를 페이지에 알리지 않아 앱이 계속 상태를 묻고(10초마다 git·
            # 5분마다 에이전트 확인) 있었다. 안 보이면 갱신을 쉬고, 다시 띄우면 visibilitychange 로 바로 새로 본다.
            self.view.page().setVisible(not self.isMinimized())
        super().changeEvent(e)


def _login_shell_path() -> None:
    """macOS·Linux 에서 Finder·런처로 연 앱은 PATH 가 /usr/bin:/bin 정도뿐이라 claude·codex·node·git(Homebrew·nvm)을 못 찾는다.
    로그인 셸이 쓰는 PATH 를 한 번 읽어 합친다 (데스크톱 개발 도구들이 흔히 쓰는 방법)."""
    if sys.platform == "win32":
        return
    import subprocess
    shell = os.environ.get("SHELL") or ("/bin/zsh" if sys.platform == "darwin" else "/bin/bash")
    try:
        out = subprocess.run([shell, "-ilc", 'printf "__OSS_PATH__%s" "$PATH"'], capture_output=True, timeout=8,
                             stdin=subprocess.DEVNULL).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return
    got = out.rsplit("__OSS_PATH__", 1)[-1].strip() if "__OSS_PATH__" in out else ""
    have = os.environ.get("PATH", "").split(os.pathsep)
    extra = [p for p in got.split(os.pathsep) if p and p not in have]
    if extra:
        os.environ["PATH"] = os.pathsep.join(have + extra)


def main(argv: list[str] | None = None) -> int:
    _windows_setup()
    _login_shell_path()
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setWindowIcon(app_icon())
    w = Window()
    w.show()
    refresh_icon_files(w.settings.home)

    def tidy():  # 예전 배치 옮기기·오래된 휴지통 비우기·남은 파일 정리 — 창을 띄운 뒤 뒤에서 (실패해도 앱은 그대로)
        try:
            from beom_workbench.core import storage
            storage.maintenance(w.settings)
        except Exception:  # noqa: BLE001
            pass
    import threading
    threading.Thread(target=tidy, daemon=True).start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
