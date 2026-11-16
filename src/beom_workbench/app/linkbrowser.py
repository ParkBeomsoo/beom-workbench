"""로그인이 필요한 링크 — 앱 안의 브라우저 창에서 사용자가 직접 로그인하고, 문서를 읽을 거리 묶음으로 가져온다.

- 내용 파악이 목적이다. 기획서는 표·목록·그림(화면 설계·흐름도)·첨부 문서가 내용의 절반이라 화면 글만 베끼지 않는다:
  본문 HTML 을 마크다운으로 바꿔 구조를 살리고(접힌 블록도 포함), 그림과 첨부 파일은 이 창의 로그인으로 받아 같은 폴더에 둔다.
- 컨플루언스·지라는 로그인한 세션으로 그 사이트의 REST API 를 불러 본문만 깨끗하게 받는다 — 페이지 + 하위 페이지 + 댓글 + 첨부,
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
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

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


class LinkBrowser(QDialog):
    """로그인 창. 가져오면 saved(url, 문서 .md 경로), 닫으면 saved(url, "")."""
    saved = Signal(str, str)

    def __init__(self, settings, url: str, parent=None):
        super().__init__(parent)
        self.settings, self.url, self._done = settings, url, False
        self._queue: list[dict] = []
        self._files: dict[str, tuple[bytes, str]] = {}
        self._doc: docs.LinkDoc | None = None
        self._dl = None
        self.setWindowTitle("로그인하고 문서 가져오기")
        self.resize(1100, 820)
        self.profile = _profile(settings)
        self.view = QWebEngineView(self)
        self.view.setPage(_Page(self.profile, self.view))
        self.info = QLabel("이 창에서 로그인해 주세요. 문서가 보이면 '이 문서 가져오기'를 누르세요 — 본문·표·그림·첨부 파일을 함께 가져옵니다.")
        self.info.setWordWrap(True)
        self.grab_btn = QPushButton("이 문서 가져오기")
        self.grab_btn.setDefault(True)
        self.grab_btn.clicked.connect(self._grab)
        cancel = QPushButton("닫기")
        cancel.clicked.connect(self.close)
        top = QHBoxLayout()
        top.addWidget(self.info, 1)
        top.addWidget(cancel)
        top.addWidget(self.grab_btn)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.addLayout(top)
        lay.addWidget(self.view, 1)
        self.view.urlChanged.connect(lambda u: self.setWindowTitle(f"로그인하고 문서 가져오기 — {u.host()}"))
        self.profile.downloadRequested.connect(self._download_requested)
        self.view.load(QUrl(url))

    # ---- 1단계: 문서 조각
    def _grab(self) -> None:
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
        shutil.rmtree(self._tmp(), ignore_errors=True)
        self._done = True
        self.saved.emit(self.url, str(f))
        self.close()

    def closeEvent(self, e) -> None:  # noqa: N802
        try:
            self.profile.downloadRequested.disconnect(self._download_requested)
        except (RuntimeError, TypeError):
            pass
        if not self._done:
            self.saved.emit(self.url, "")
            self._done = True
        super().closeEvent(e)
