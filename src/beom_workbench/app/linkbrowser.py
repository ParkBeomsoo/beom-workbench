"""로그인이 필요한 링크 — 앱 안의 브라우저 창에서 사용자가 직접 로그인하고, 문서를 읽을 거리 묶음으로 가져온다.

- 내용 파악이 목적이다. 기획서는 표·목록·그림(화면 설계·흐름도)·첨부 문서가 내용의 절반이라 화면 글만 베끼지 않는다:
  본문 HTML 을 마크다운으로 바꿔 구조를 살리고(접힌 블록도 포함), 그림과 첨부 파일은 이 창의 로그인으로 받아 같은 폴더에 둔다.
- 위키 문서·이슈는 페이지 자체(주소·페이지 정보)로 알아보고, 로그인한 세션으로 그 사이트의 REST API 를 불러 본문만 깨끗하게 받는다 — 페이지 + 하위 페이지 + 댓글 + 첨부,
  이슈 + 하위 작업 + 연결된 이슈 + 댓글 + 첨부. API 가 막혀 있으면 보이는 페이지의 본문 영역을 같은 방식으로 바꾼다.
- 로그인 정보(쿠키)는 앱 화면과 분리된 전용 프로필에 남는다 (툴킷 홈의 cache/browser). 다음에 같은 사이트는 다시 로그인하지 않는다.
- 이 창에는 앱의 내부 통로(QWebChannel)가 없다 — 외부 페이지가 앱 기능을 부를 수 없다.
- 비밀번호는 사용자가 이 창에 직접 넣는다. 앱은 입력값을 읽지 않는다.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEngineDownloadRequest, QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from beom_workbench.core import docs

_PROFILE: dict = {}

# 1단계 — 문서의 HTML 조각·메타·첨부 목록. 비동기라 결과는 window.__wbGrab 에 두고 창이 받아 간다.
_GRAB = r"""(() => {
  window.__wbGrab = null;
  const meta = (n) => ((document.querySelector(`meta[name="${n}"]`) || {}).content || '');
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
  const done = (r) => { window.__wbGrab = JSON.stringify({ ...r, url: location.href,
    password: !!document.querySelector('input[type=password]') }); };
  const get = (u) => fetch(u, { credentials: 'include', headers: { Accept: 'application/json' } })
    .then((r) => r.ok ? r.json() : Promise.reject(r.status));
  const soft = (u) => get(u).catch(() => ({ results: [] }));
  const page = () => {
    const pick = document.querySelector('#main-content, [data-testid="issue.views.issue-details.issue-layout"], main, article, [role=main]');
    const el = pick && pick.innerText.trim().length > 200 ? pick : document.body;
    return { how: 'page-login', title: document.title || '', base: location.href,
             parts: [{ heading: '', html: (el ? el.outerHTML : '').slice(0, 6000000) }], meta: [], attachments: [] };
  };
  const href = location.href, ctx = meta('ajs-context-path');
  const pid = (href.match(/\/pages\/(\d+)/) || href.match(/[?&]pageId=(\d+)/) || [])[1] || meta('ajs-page-id');
  const key = (href.match(/\/browse\/([A-Z][A-Z0-9_]+-\d+)/) || href.match(/[?&]selectedIssue=([A-Z][A-Z0-9_]+-\d+)/) || [])[1]
    || meta('ajs-issue-key');
  if (pid) {
    const root = location.origin + (ctx || (href.includes('/wiki/') ? '/wiki' : ''));
    const api = `${root}/rest/api/content/${pid}`;
    get(`${api}?expand=body.view,space,version,ancestors`).then(async (j) => {
      const [kids, comments, files] = await Promise.all([
        soft(`${api}/child/page?limit=25&expand=body.view`),
        soft(`${api}/child/comment?limit=100&depth=all&expand=body.view,history`),
        soft(`${api}/child/attachment?limit=100`)]);
      const view = (x) => (((x || {}).body || {}).view || {}).value || '';
      const cm = (comments.results || []).map((c, i) =>
        `<h3>댓글 ${i + 1} — ${esc(((c.history || {}).createdBy || {}).displayName)} (${esc((c.history || {}).createdDate)})</h3>${view(c)}`);
      done({ how: 'confluence-api', title: j.title, base: location.origin + '/',
        meta: [`공간: ${(j.space || {}).name || ''} · 버전 ${(j.version || {}).number || ''}` +
               ((j.ancestors || []).length ? ` · 위치: ${j.ancestors.map((a) => a.title).join(' > ')}` : '')],
        parts: [{ heading: '', html: view(j) }]
          .concat((kids.results || []).map((k) => ({ heading: `하위 페이지 — ${k.title}`, html: view(k) })))
          .concat(cm.length ? [{ heading: '댓글', html: cm.join('') }] : []),
        attachments: (files.results || []).map((a) => ({ name: a.title, url: root + ((a._links || {}).download || ''),
          media: (a.extensions || {}).mediaType || (a.metadata || {}).mediaType || '', size: (a.extensions || {}).fileSize || 0 })) });
    }).catch(() => done(page()));
  } else if (key) {
    const root = location.origin + ctx;
    get(`${root}/rest/api/2/issue/${key}?expand=renderedFields&fields=summary,description,status,issuetype,priority,labels,` +
        'comment,assignee,reporter,attachment,subtasks,issuelinks,parent,components,fixVersions').then((j) => {
      const f = j.fields || {}, rf = j.renderedFields || {};
      const brief = (o) => `${esc(o.key)} ${esc((o.fields || {}).summary)} [${esc(((o.fields || {}).status || {}).name)}]`;
      const links = (f.issuelinks || []).map((l) => {
        const o = l.outwardIssue || l.inwardIssue;
        return o ? `<li>${esc(l.outwardIssue ? (l.type || {}).outward : (l.type || {}).inward)}: ${brief(o)}</li>` : '';
      }).join('');
      const subs = (f.subtasks || []).map((s) => `<li>${brief(s)}</li>`).join('');
      const cm = ((rf.comment || {}).comments || []).map((c, i) =>
        `<h3>댓글 ${i + 1} — ${esc((c.author || {}).displayName)} (${esc(c.created)})</h3>${c.body || ''}`).join('');
      const names = (xs) => (xs || []).map((x) => x.name).join(', ');
      done({ how: 'jira-api', title: `${j.key} ${f.summary || ''}`, base: location.origin + '/',
        meta: [`유형: ${(f.issuetype || {}).name || ''} · 상태: ${(f.status || {}).name || ''} · 우선순위: ${(f.priority || {}).name || ''}` +
                 ((f.labels || []).length ? ` · 라벨: ${f.labels.join(', ')}` : ''),
               `담당: ${(f.assignee || {}).displayName || '없음'} · 보고: ${(f.reporter || {}).displayName || ''}` +
                 (f.parent ? ` · 상위: ${f.parent.key} ${((f.parent.fields || {}).summary) || ''}` : '') +
                 (names(f.components) ? ` · 구성요소: ${names(f.components)}` : '') +
                 (names(f.fixVersions) ? ` · 수정 버전: ${names(f.fixVersions)}` : '')],
        parts: [{ heading: '설명', html: rf.description || `<pre>${esc(typeof f.description === 'string' ? f.description : '')}</pre>` },
                { heading: '하위 작업', html: subs ? `<ul>${subs}</ul>` : '' },
                { heading: '연결된 이슈', html: links ? `<ul>${links}</ul>` : '' },
                { heading: '댓글', html: cm }],
        attachments: (f.attachment || []).map((a) => ({ name: a.filename, url: a.content, media: a.mimeType, size: a.size })) });
    }).catch(() => done(page()));
  } else {
    done(page());
  }
  return 'started';
})()"""


_COOKIES: dict = {}
# 패널 겉모습 — 앱(app.css)의 '먹과 호박'과 같은 색·글꼴·모서리 (기본 Qt 단추는 앱과 따로 놀았다)
PANE_QSS = """
#pane { background: #f7f6f3; border-left: 1px solid rgba(40, 34, 24, .14); }
#pane QLabel { color: #505a56; font-family: "Pretendard Variable", "Pretendard", "Malgun Gothic", "Apple SD Gothic Neo"; font-size: 12px; }
#pane QLabel#title { color: #1a1f1d; font-size: 14px; font-weight: 700; }
#pane QLabel#host { color: #6b706d; font-size: 12px; }
#pane QFrame#hint { background: #ffffff; border: 1px solid rgba(40, 34, 24, .12); border-radius: 10px; }
#pane QFrame#hint QLabel { color: #505a56; font-size: 12px; }
#pane QFrame#line { background: rgba(40, 34, 24, .12); max-height: 1px; min-height: 1px; border: 0; }
#pane QLineEdit { border: 1px solid rgba(40, 34, 24, .18); border-radius: 15px; padding: 5px 12px; background: #efede8; color: #1a1f1d;
  font-family: "Pretendard Variable", "Pretendard", "Malgun Gothic", "Apple SD Gothic Neo"; font-size: 13px; selection-background-color: #f0b55a; min-height: 18px; }
#pane QLineEdit:hover { background: #ebe8e2; }
#pane QLineEdit:focus { border: 2px solid #a85a08; padding: 4px 11px; background: #ffffff; }
#pane QPushButton { border: 1px solid rgba(40, 34, 24, .2); border-radius: 8px; padding: 6px 12px; background: #ffffff; color: #1a1f1d;
  font-family: "Pretendard Variable", "Pretendard", "Malgun Gothic", "Apple SD Gothic Neo"; font-size: 13px; font-weight: 600; min-height: 20px; }
#pane QPushButton:hover { background: #efede8; }
#pane QPushButton:focus { border: 2px solid #a85a08; padding: 5px 11px; }
#pane QPushButton#primary { background: #a85a08; border-color: #a85a08; color: #ffffff; }
#pane QPushButton#primary:hover { background: #bd6a12; }
#pane QPushButton#allow:checked { background: #fdf1e1; border-color: #d99a4a; color: #7a4206; }
#pane QPushButton:disabled { color: #8a8f8c; background: #efede8; border-color: rgba(40, 34, 24, .12); }
#pane QToolButton { border: 0; border-radius: 8px; background: transparent; padding: 0; }
#pane QToolButton:hover { background: #e9e6e0; }
#pane QToolButton:pressed { background: #dedad2; }
#pane QToolButton:focus { border: 2px solid #a85a08; }
"""

# 앱 화면과 같은 선 아이콘 (24 칸, 1.7 굵기) — Qt 기본 아이콘은 운영체제마다 모양이 달라 패널만 낯설어 보였다
_ICONS = {
    "back": '<path d="M14.5 6 8.5 12l6 6"/>',
    "fwd": '<path d="M9.5 6l6 6-6 6"/>',
    "reload": '<path d="M19 12a7 7 0 1 1-2.05-4.95"/><path d="M19 4.5v4h-4"/>',
    "close": '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
    "globe": '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.4 2.3 3.6 5.1 3.6 8.5s-1.2 6.2-3.6 8.5M12 3.5C9.6 5.8 8.4 8.6 8.4 12s1.2 6.2 3.6 8.5"/>',
    "camera": '<path d="M4 8.5h3l1.6-2.5h6.8L17 8.5h3v10H4z"/><circle cx="12" cy="13" r="3.3"/>',
    "shield": '<path d="M12 3.5 19 6v5.5c0 4.2-3 7.6-7 9-4-1.4-7-4.8-7-9V6z"/>',
}


_BLANK = """<!doctype html><meta charset="utf-8"><style>
html,body{height:100%;margin:0;background:#fbfaf8;font:13px/1.6 "Pretendard Variable",Pretendard,"Malgun Gothic","Apple SD Gothic Neo",sans-serif;color:#505a56}
.b{height:100%;display:flex;align-items:center;justify-content:center;padding:24px;box-sizing:border-box}
.c{max-width:320px;text-align:center}.c b{display:block;font-size:15px;color:#1a1f1d;margin-bottom:6px}
ol{text-align:left;margin:14px 0 0;padding-left:20px}li{margin:4px 0}
</style><div class="b"><div class="c"><b>위 주소창에 사이트 주소를 넣으세요</b>
로그인이 필요한 관리자 화면·문서를 여기서 열고
<ol><li>필요하면 이 안에서 로그인</li><li>원하는 화면에서 '이 화면 가져오기' — 대화에 첨부</li>
<li>에이전트가 직접 찍어야 하면 '에이전트도 이 사이트 보기'</li></ol></div></div>"""


def _icon(name: str, color: str = "#3d4441", size: int = 18):
    from PySide6.QtCore import QByteArray, Qt
    from PySide6.QtGui import QIcon, QPainter, QPixmap
    from PySide6.QtSvg import QSvgRenderer
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.7" '
           f'stroke-linecap="round" stroke-linejoin="round">{_ICONS[name]}</svg>')
    pm = QPixmap(size * 3, size * 3)  # 고해상도 화면에서도 또렷하게
    pm.fill(Qt.transparent)
    pa = QPainter(pm)
    QSvgRenderer(QByteArray(svg.encode())).render(pa)
    pa.end()
    pm.setDevicePixelRatio(3)
    return QIcon(pm)


def _ckey(c) -> tuple:
    return (bytes(c.name()).decode("utf-8", "replace"), c.domain(), c.path())


def _cdict(c) -> dict:
    exp = c.expirationDate()
    return {"name": bytes(c.name()).decode("utf-8", "replace"), "value": bytes(c.value()).decode("utf-8", "replace"), "domain": c.domain(),
            "path": c.path() or "/", "secure": c.isSecure(), "httpOnly": c.isHttpOnly(),
            **({"expires": int(exp.toSecsSinceEpoch())} if exp.isValid() else {})}


def _profile(settings) -> QWebEngineProfile:
    if "p" not in _PROFILE:
        from beom_workbench.core import storage
        root = storage.cache_dir(settings) / "browser"
        root.mkdir(parents=True, exist_ok=True)
        prof = QWebEngineProfile("beom-docs-login")  # 이름 있는 프로필 = 디스크에 남는다 (쿠키·로그인 유지)
        prof.setPersistentStoragePath(str(root / "storage"))
        prof.setCachePath(str(root / "cache"))
        prof.setHttpCacheMaximumSize(50 * 1024 * 1024)  # 문서 몇 개 읽는 창 — 캐시가 끝없이 커지지 않게
        prof.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
        store = prof.cookieStore()
        store.cookieAdded.connect(lambda c: _COOKIES.__setitem__(_ckey(c), _cdict(c)))
        store.cookieRemoved.connect(lambda c: _COOKIES.pop(_ckey(c), None))
        store.loadAllCookies()
        _PROFILE["p"] = prof
    return _PROFILE["p"]


class _Page(QWebEnginePage):
    def createWindow(self, _type):  # noqa: N802 — 로그인 팝업(SSO)은 같은 창에서 연다
        return self

    def acceptNavigationRequest(self, url, nav_type, is_main):  # noqa: N802
        return url.scheme() in ("http", "https", "about", "data", "blob")


def links_dir(settings) -> Path:
    from beom_workbench.core import storage
    return storage.cache_dir(settings) / "links"


def build_doc(data: dict) -> docs.LinkDoc:
    """1단계 결과(JSON) → LinkDoc."""
    doc = docs.LinkDoc(data.get("url") or "", data.get("title") or "", data.get("how") or "page-login")
    doc.meta = [m for m in data.get("meta") or [] if m and m.strip(" ·:")]
    base = data.get("base") or doc.url
    for i, part in enumerate(data.get("parts") or []):
        title = doc.add_html(part.get("heading") or "", part.get("html") or "", base, whole_page=doc.how == "page-login" and i == 0)
        if not doc.title and title:
            doc.title = title
    doc.attachments = [a for a in data.get("attachments") or [] if a.get("url")]
    return doc


def text_len(doc: docs.LinkDoc) -> int:
    return sum(len(re.sub(r"\x00IMG\d+\x00", "", md).strip()) for _, md in doc.parts)


class LinkBrowser(QWidget):
    """앱 오른쪽 브라우저 패널 — 로그인하고 지금 보이는 화면·문서를 가져온다(saved(첨부 키, .md 경로)), 그 사이트를 에이전트가 찍게
    허락한다(로그인 상태를 툴킷 홈에 — browser_auth). 닫으면 saved(키, "") 와 closed. 대화 옆 패널에 두어 대화를 보면서 쓴다."""
    saved = Signal(str, str)
    closed = Signal()

    def __init__(self, settings, url: str = "", parent=None):
        super().__init__(parent)
        self.settings, self.url, self._done = settings, url, False
        self._queue: list[dict] = []
        self._files: dict[str, tuple[bytes, str]] = {}
        self._doc: docs.LinkDoc | None = None
        self._dl = None
        self.profile = _profile(settings)
        self.view = QWebEngineView(self)
        self.view.setPage(_Page(self.profile, self.view))
        from PySide6.QtCore import QSize
        from PySide6.QtWidgets import QFrame, QToolButton

        def tool(name, tip, fn):
            b = QToolButton()
            b.setIcon(_icon(name))
            b.setIconSize(QSize(18, 18))
            b.setFixedSize(30, 30)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            return b
        # 1줄: 제목 — 오른쪽 끝에 닫기 (머리줄의 '브라우저' 단추를 다시 눌러도 닫힌다)
        mark = QLabel()
        mark.setPixmap(_icon("globe", "#a85a08", 16).pixmap(16, 16))
        title = QLabel("브라우저")
        title.setObjectName("title")
        self.host = QLabel("")
        self.host.setObjectName("host")
        close = tool("close", "브라우저 닫기 — 위 '브라우저' 단추를 다시 눌러도 닫힙니다", self.close_pane)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(mark)
        head.addWidget(title)
        head.addWidget(self.host, 1)
        head.addWidget(close)
        # 2줄: 이동 — 뒤로·앞으로·새로 고침 + 둥근 주소창
        self.back_btn = tool("back", "뒤로", self.view.back)
        self.fwd_btn = tool("fwd", "앞으로", self.view.forward)
        reload_ = tool("reload", "새로 고침", self.view.reload)
        self.addr = QLineEdit()
        self.addr.setPlaceholderText("주소를 넣고 Enter — 예: admin.example.com")
        self.addr.returnPressed.connect(self._go)
        nav = QHBoxLayout()
        nav.setSpacing(2)
        for w in (self.back_btn, self.fwd_btn, reload_):
            nav.addWidget(w)
        nav.addSpacing(6)
        nav.addWidget(self.addr, 1)
        # 3줄: 안내와 할 일 — 카드 하나에
        self.info = QLabel("로그인이 필요하면 아래 화면에서 로그인하세요. 원하는 화면에서 '이 화면 가져오기'를 누르면 화면 모습과 내용이 첨부됩니다.")
        self.info.setWordWrap(True)
        self.grab_btn = QPushButton(_icon("camera", "#ffffff", 16), " 이 화면 가져오기")
        self.grab_btn.setDefault(True)
        self.grab_btn.clicked.connect(self._grab)
        self.allow_btn = QPushButton(_icon("shield", "#7a4206", 16), " 에이전트도 이 사이트 보기")
        self.allow_btn.setObjectName("allow")
        self.allow_btn.setCheckable(True)
        self.allow_btn.setToolTip("이 사이트에 로그인한 상태로 에이전트가 필요한 메뉴 화면을 찍게 합니다 (12시간). 비밀번호·쿠키는 에이전트에게 가지 않습니다.")
        self.allow_btn.clicked.connect(self._toggle_allow)
        acts = QHBoxLayout()
        acts.setSpacing(6)
        acts.addWidget(self.grab_btn)
        acts.addWidget(self.allow_btn)
        acts.addStretch(1)
        self.grab_btn.setObjectName("primary")
        card = QFrame()
        card.setObjectName("hint")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(12, 10, 12, 10)
        cl.setSpacing(8)
        cl.addWidget(self.info)
        cl.addLayout(acts)
        line = QFrame()
        line.setObjectName("line")
        self.setObjectName("pane")
        from PySide6.QtCore import Qt as _Qt
        self.setAttribute(_Qt.WA_StyledBackground, True)  # 없으면 #pane 배경이 칠해지지 않고 운영체제 회색이 보인다
        self.setStyleSheet(PANE_QSS)
        top = QVBoxLayout()
        top.setContentsMargins(14, 11, 10, 10)
        top.setSpacing(9)
        top.addLayout(head)
        top.addLayout(nav)
        top.addWidget(card)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(top)
        lay.addWidget(line)
        lay.addWidget(self.view, 1)  # 웹 화면은 패널 끝까지 — 여백 없이
        self.view.urlChanged.connect(self._url_changed)
        self.profile.downloadRequested.connect(self._download_requested)
        if url:
            self.view.load(QUrl(url))
        else:
            self._blank()

    def _blank(self) -> None:
        """주소 없이 열었을 때 — 흰 빈칸 대신 무엇을 하면 되는지."""
        self.view.setHtml(_BLANK)
        self.addr.clear()
        self.host.clear()
        self.grab_btn.setEnabled(False)  # 가져올 화면이 아직 없다 (안내 화면은 urlChanged 가 늦거나 오지 않는다)

    def open(self, url: str, key: str = "") -> None:
        """다른 주소로 — key 는 가져오면 바꿀 첨부(로그인이 필요했던 링크)."""
        self.url, self._done = key or url, False
        self.grab_btn.setEnabled(True)
        if url:
            self.view.load(QUrl(url))
        elif not self.view.url().host():
            self._blank()

    def _go(self) -> None:
        u = self.addr.text().strip()
        if u and not re.match(r"https?://", u):
            u = "https://" + u
        if u:
            self.view.load(QUrl(u))

    def _host(self) -> str:
        return self.view.url().host().lower()

    def _url_changed(self, u) -> None:
        web = u.scheme() in ("http", "https")
        self.addr.setText(u.toString() if web else "")  # 안내 화면(about:blank·data:)의 주소는 보이지 않게
        self.host.setText(u.host() if web else "")
        self.grab_btn.setEnabled(web)
        h = self.view.history()
        self.back_btn.setEnabled(h.canGoBack())
        self.fwd_btn.setEnabled(h.canGoForward())
        self._sync_allow()

    def _sync_allow(self) -> None:
        from beom_workbench.core import browser_auth
        host = self._host()
        on = any(h["host"] == host for h in browser_auth.hosts(self.settings)) if host else False
        self.allow_btn.setChecked(on)  # 켜져 있으면 눌린 모양 — 다시 누르면 끊는다
        self.allow_btn.setText(" 에이전트 허락됨 — 눌러서 끊기" if on else " 에이전트도 이 사이트 보기")
        self.allow_btn.setEnabled(bool(host))

    def _toggle_allow(self) -> None:
        from beom_workbench.core import browser_auth
        host = self._host()
        if not host:
            return
        if any(h["host"] == host for h in browser_auth.hosts(self.settings)):
            browser_auth.forget(self.settings, host)
            self.info.setText(f"{host} — 에이전트 허락을 끊었습니다.")
        else:
            got = browser_auth.allow(self.settings, host, list(_COOKIES.values()))
            self.info.setText(f"{host} — 에이전트가 로그인한 상태로 이 사이트의 화면을 찍을 수 있습니다 (12시간, 쿠키 {got['cookies']}개). "
                              "로그인을 마친 뒤에 눌러야 그 상태가 쓰입니다.")
        self._sync_allow()

    def close_pane(self) -> None:
        if not self._done and self.url:
            self.saved.emit(self.url, "")
        self._done = True
        self.hide()
        self.closed.emit()

    # ---- 1단계: 문서 조각
    def _grab(self) -> None:
        self._shot = self.view.grab()  # 누른 순간 보이는 화면 — 관리자 화면처럼 글보다 모양이 중요한 페이지의 근거
        self.grab_btn.setEnabled(False)
        self.info.setText("가져오는 중…")
        self._polls = 0
        self.view.page().runJavaScript(_GRAB, 0, lambda _r: QTimer.singleShot(200, self._poll))

    def _poll(self) -> None:
        self._polls += 1
        self.view.page().runJavaScript("window.__wbGrab", 0, self._polled)

    def _polled(self, raw) -> None:
        if raw is None and self._polls < 150:  # 최대 30초 (하위 페이지·댓글까지)
            QTimer.singleShot(200, self._poll)
            return
        self._got(raw)

    def _got(self, raw) -> None:
        try:
            data = json.loads(raw or "{}")
        except (TypeError, ValueError):
            data = {}
        if not data:
            return self._retry("문서를 읽지 못했습니다 — 페이지가 다 열린 뒤 다시 눌러 주세요.")
        doc = build_doc(data)
        api = doc.how in ("confluence-api", "jira-api")
        n = text_len(doc)
        if not api and (data.get("password") and n < 400 or n < 40):
            return self._retry("아직 로그인 화면이거나 내용이 거의 없습니다 — 로그인을 마치고 문서가 보이면 다시 눌러 주세요.")
        self._doc = doc
        self._queue = doc.wanted()
        self._total = len(self._queue)
        self._next_file()

    def _retry(self, msg: str) -> None:
        self.grab_btn.setEnabled(True)
        self.info.setText(msg)

    # ---- 2단계: 그림·첨부 — 이 창의 로그인으로 받는다 (브라우저 내려받기라 다른 주소로 넘겨도 된다)
    def _tmp(self) -> Path:
        d = links_dir(self.settings) / ".part"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _next_file(self) -> None:
        if not self._queue:
            return self._finish()
        w = self._queue.pop(0)
        self._want = w["url"]
        self.info.setText(f"그림·첨부 파일 받는 중… {self._total - len(self._queue)}/{self._total}")
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._file_timeout)
        self._timer.start(30000)
        self.view.page().download(QUrl(w["url"]), f"f{len(self._files)}-{int(time.time() * 1000)}")

    def _download_requested(self, req: QWebEngineDownloadRequest) -> None:
        if self._doc is None or not getattr(self, "_want", None):
            req.cancel()  # 사용자가 페이지에서 누른 내려받기 등 — 이 창은 받지 않는다
            return
        if req.totalBytes() > docs.FILE_MAX:
            req.cancel()
            return self._file_done(None)
        req.setDownloadDirectory(str(self._tmp()))
        req.isFinishedChanged.connect(lambda r=req: self._file_finished(r))
        self._dl = req
        req.accept()

    def _file_finished(self, req) -> None:
        if req is not self._dl:
            return
        data = None
        p = Path(req.downloadDirectory()) / req.downloadFileName()
        if req.state() == QWebEngineDownloadRequest.DownloadCompleted and p.is_file() and p.stat().st_size <= docs.FILE_MAX:
            data = (p.read_bytes(), req.mimeType())
        p.unlink(missing_ok=True)
        self._file_done(data)

    def _file_timeout(self) -> None:
        if self._dl is not None:
            self._dl.cancel()
        self._file_done(None)

    def _file_done(self, data) -> None:
        if getattr(self, "_timer", None):
            self._timer.stop()
        if data and self._want:
            self._files[self._want] = data
        self._want, self._dl = None, None
        QTimer.singleShot(0, self._next_file)

    def _finish(self) -> None:
        doc = self._doc
        slug = re.sub(r"[^\w가-힣-]+", "-", doc.title or "page")[:40].strip("-") or "page"
        folder = links_dir(self.settings) / f"{slug}-{time.strftime('%Y%m%d-%H%M%S')}"
        f = doc.write(folder, self._files)
        shot = getattr(self, "_shot", None)
        if shot is not None and not shot.isNull() and shot.save(str(Path(f).parent / "화면.png"), "PNG"):
            with open(f, "a", encoding="utf-8") as out:  # 읽을 거리에 함께 — 에이전트가 그림으로 읽는다
                out.write("\n\n## 가져올 때 보이던 화면\n\n로그인한 창에서 '가져오기'를 누른 순간의 모습이다 — 이 화면의 틀·메뉴·버튼·목록 배치를 따른다.\n\n"
                          "![가져올 때 보이던 화면](화면.png)\n")
        shutil.rmtree(self._tmp(), ignore_errors=True)
        self.saved.emit(self.url, str(f))
        self.url, self._doc, self._files = "", None, {}  # 패널은 열린 채 — 다음 화면도 가져올 수 있다
        self.grab_btn.setEnabled(True)
        self.info.setText("가져왔습니다 — 대화 입력창의 첨부에 넣었습니다. 다른 화면도 가져올 수 있습니다.")
