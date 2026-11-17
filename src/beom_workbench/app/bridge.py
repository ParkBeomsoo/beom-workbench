"""화면(웹 기술) ↔ 툴킷 연결 — Qt WebChannel 로 같은 프로세스 안에서만 통한다. 네트워크 포트를 열지 않는다.

모든 슬롯은 JSON 문자열을 받고 돌려준다: {"ok": true, "data": …} 또는 {"ok": false, "error": "…"}.
"""
from __future__ import annotations

import getpass
import json
import os
import subprocess
import re
import shutil
import sys
import threading
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog

from beom_workbench import __version__
from beom_workbench.app import view
from beom_workbench.common.response import ToolError
from beom_workbench.core import agents, inbox, integrity, inventory, port, project, rounds, runner
from beom_workbench.core import port_guard as guard
from beom_workbench.core.checkpoints import Checkpoints


def _ok(data=None) -> str:
    return json.dumps({"ok": True, "data": data}, ensure_ascii=False, default=str)


def _err(e: Exception) -> str:
    msg = getattr(e, "message", None) or f"{type(e).__name__}: {e}"
    return json.dumps({"ok": False, "error": msg}, ensure_ascii=False)


def safe(fn):
    def wrap(self, *a):
        try:
            return _ok(fn(self, *a))
        except Exception as e:  # noqa: BLE001 — 화면에는 오류 문장만
            return _err(e)
    wrap.__name__ = fn.__name__
    return wrap


NOTICE_ASK = "확인이 필요합니다"
AGENT_CHECK_SEC = 300


def notice(row: dict) -> str:
    """창 밖 알림 문구 — 사람이 볼 일이 생겼을 때만. 사람이 직접 한 일(수락해서 '완료')은 알리지 않는다."""
    if row.get("archived"):
        return ""
    if row.get("state") == "done":
        return "" if row["status"] == "완료" else "끝났습니다 — 결과를 보고 수락해 주세요"
    return {"내 차례": NOTICE_ASK, "확인 필요": "멈췄습니다 — 확인해 주세요",
            "실패": "실패했습니다", "로그인 필요": "로그인이 필요합니다"}.get(row.get("status"), "")


def _sandbox_status() -> dict:
    try:
        from beom_workbench.core import sandbox
        return sandbox.status()
    except Exception:  # noqa: BLE001
        return {"ready": False}


REQUIRED_ENGINES = ("codebase-memory-mcp", "ast-grep")  # 레퍼런스 모드의 원본 분석에 꼭 필요한 것


def _engine_status(settings) -> dict:
    """레퍼런스 모드의 분석 엔진 — 없으면 처음 설정에서 설치할 명령."""
    from beom_workbench.adapters.astgrep import AstGrep
    from beom_workbench.adapters.cbm import CodebaseMemory
    have = {"codebase-memory-mcp": bool(CodebaseMemory(settings).version()), "ast-grep": bool(AstGrep(settings).version())}
    missing = [k for k in REQUIRED_ENGINES if not have[k]]
    if not missing:
        return {"ready": True}
    py = sys.executable
    if Path(py).name.lower() == "pythonw.exe" and (Path(py).parent / "python.exe").is_file():
        py = str(Path(py).parent / "python.exe")
    q = (lambda s: "'" + s.replace("'", "''") + "'") if os.name == "nt" else shlex_quote
    cmd = f"{'& ' if os.name == 'nt' else ''}{q(py)} -m beom_workbench.cli setup --apply {' '.join(missing)}"
    return {"ready": False, "missing": missing, "install": cmd,
            "note": "레퍼런스 모드가 원본 저장소를 분석할 때 쓰는 엔진입니다 (공식 배포본을 체크섬으로 확인해 설치)"}


def shlex_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


def _open_terminal(cmd, shell: bool = False) -> None:
    """사용자가 보는 터미널 창에서 명령을 연다 (로그인·설치처럼 직접 입력·확인이 필요한 것). cmd: 인자 목록 또는 셸 한 줄."""
    import shlex
    line = cmd if isinstance(cmd, str) else subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd)
    if os.name == "nt":
        if shell or isinstance(cmd, str):
            subprocess.Popen(["powershell", "-NoExit", "-NoProfile", "-Command", line], creationflags=subprocess.CREATE_NEW_CONSOLE)
        else:
            subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE)
    elif sys.platform == "darwin":
        script = line.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{script}"', "-e", 'tell application "Terminal" to activate'])
    else:
        term = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal") or shutil.which("konsole") or shutil.which("xterm")
        if not term:
            raise ToolError("not_found", f"터미널을 찾지 못했습니다 — 직접 실행해 주세요: {line}")
        subprocess.Popen([term, "-e", "bash", "-lc", line + "; exec bash"])


def _unique_dest(folder: Path, src: Path) -> Path:
    """folder 안에 src 를 둘 이름 — 같은 이름이 있고 내용이 다르면 name-2.ext, name-3.ext …"""
    import filecmp

    dest = folder / src.name
    n = 2
    while dest.exists() and not (dest.is_file() and filecmp.cmp(dest, src, shallow=False)):
        dest = folder / f"{src.stem}-{n}{src.suffix}"
        n += 1
    return dest


class Api(QObject):
    created = Signal(str)       # JSON {"run_id"} 또는 {"error"}
    notify = Signal(str, str)   # 제목, 내용 — 창 밖 알림
    attention = Signal()        # 알림은 이미 띄웠고 아직 안 봤다 — 작업 표시줄만 깜빡인다
    agentsChanged = Signal(str)  # 에이전트 설치·로그인 상태 (뒤에서 확인이 끝났을 때)
    exportDone = Signal(str)     # PDF 내보내기가 끝남 — JSON {ok, path, folder}
    linkChecked = Signal(str)    # 첨부 링크를 미리 읽어 봄 — JSON {url, ok, needs_login, error, title}
    linkSaved = Signal(str)      # 앱 브라우저에서 로그인해 가져옴 — JSON {url, path} (path 빈 값 = 닫음)
    filesDropped = Signal(str)   # 창에 끌어다 놓은 파일 — JSON [경로] (창이 브라우저처럼 그 파일로 넘어가지 않게 가로챈다)
    updateFound = Signal(str)    # 새 버전이 나옴 — JSON {version, url}

    def __init__(self, settings, window=None):
        super().__init__()
        self.settings = settings
        self.window = window
        integrity.enable()  # 이 프로세스(앱)가 사람 결정의 유일한 권한자 — 라운드 뒤 앱 밖에서 바뀐 결정을 잡는다
        self.runners: dict[str, runner.Runner] = {}
        self.threads: dict[str, threading.Thread] = {}
        self._last_status: dict[str, str] = {}    # 실행 → 마지막으로 본 알림 문구 ("" = 알릴 것 없음)
        self._unseen: dict[str, str] = {}         # 실행 → 사용자가 아직 못 본 창 밖 알림 문구

    # -- 읽기
    @Slot(result=str)
    @safe
    def bootstrap(self):
        self._refresh_agents()
        return {"version": __version__, "agents": self._agents_now(), "runs": view.runs(self.settings),
                "defaults": self._prefs().get("defaults") or {},
                "kinds": inbox.KINDS, "default_policy": inbox.DEFAULT_POLICY, "sandbox": _sandbox_status(),
                "permission_modes": [{"id": k, "label": m["label"], "desc": m["desc"], "policy": m["policy"]}
                                     for k, m in inbox.PERMISSION_MODES.items()],
                "i18n": self._i18n()}

    def _i18n(self) -> dict:
        """화면 언어 — 고른 값(auto·ko·en·ja·zh), 실제로 보일 언어, 그 언어의 번역표 (한국어가 원문이라 한국어는 빈 표)."""
        from beom_workbench import i18n
        choice = self._prefs().get("lang") or "auto"
        lang = i18n.resolve(choice)
        return {"choice": choice, "lang": lang, "system": i18n.system_language(), "languages": i18n.LANGUAGES,
                "catalog": i18n.catalog(lang)}

    def lang(self) -> str:
        from beom_workbench import i18n
        return i18n.resolve(self._prefs().get("lang") or "auto")

    @Slot(str, result=str)
    @safe
    def setLanguage(self, choice):
        """화면 언어를 바꾼다 — 'auto' 는 운영체제 언어를 따르고, 지원하지 않는 언어면 한국어."""
        from beom_workbench import i18n
        from beom_workbench.core import storage
        if choice != "auto" and choice not in i18n.LANGUAGES:
            raise ToolError("invalid_request", f"지원하지 않는 언어: {choice}")
        storage.save_prefs(self.settings, {"lang": choice})
        return self._i18n()

    @Slot(result=str)
    @safe
    def listRuns(self):
        rows = view.runs(self.settings)
        level = rounds.get(self.settings)["notify"]
        for r in rows:
            prev = self._last_status.get(r["id"])
            text = notice(r)
            self._last_status[r["id"]] = text
            if prev is None or prev == text or not text or level == "off":
                continue
            important = text != NOTICE_ASK
            if level == "important" and not important:
                continue
            # 사용자가 아직 못 본 알림이 있는 작업에는 같은 알림을 또 띄우지 않는다 — 작업 표시줄만 깜빡인다.
            # 끝남·실패처럼 다른 일이면 띄운다. 창을 다시 보면(seen) 처음부터 다시 센다.
            if self._unseen.get(r["id"]) == text:
                self.attention.emit()
                continue
            self._unseen[r["id"]] = text
            from beom_workbench import i18n
            lang = self.lang()
            self.notify.emit(r["name"] or i18n.tr("프로젝트", lang), i18n.tr(text, lang))
        return rows

    def seen(self) -> None:
        """사용자가 창을 다시 봤다 — 못 본 알림 기록을 비운다 (창 쪽에서 부른다)."""
        self._unseen.clear()

    @Slot(str, result=str)
    @safe
    def runDetail(self, run_id):
        d = view.detail(self.settings, run_id)
        d["running"] = self._busy(run_id)
        return d

    @Slot(str, result=str)
    @safe
    def changes(self, run_id):
        return view.changes(self.settings, run_id)

    @Slot(result=str)
    @safe
    def agentStatus(self):
        """화면은 바로 받는다 (마지막으로 확인한 값). 5분이 지났으면 뒤에서 다시 확인하고 agentsChanged 로 알린다.
        확인은 에이전트 CLI 를 여러 번 띄우는 일이라 자주 하지 않는다 — 로그인 버튼을 누른 뒤에는 따로 바로 확인한다."""
        if time.monotonic() - getattr(self, "_agents_at", 0) > AGENT_CHECK_SEC:
            self._refresh_agents()
        return self._agents_now()

    def _agents_cache_file(self) -> Path:
        from beom_workbench.core import storage
        return storage.cache_dir(self.settings) / "agents.json"

    def _agents_now(self) -> list:
        if getattr(self, "_agents", None):
            return self._agents
        try:  # 지난번 앱에서 확인한 값 — 시작을 몇 초 늦추지 않게
            cached = json.loads(self._agents_cache_file().read_text(encoding="utf-8"))
            if isinstance(cached, list) and cached:
                return cached
        except (OSError, ValueError):
            pass
        if not getattr(self, "_agents_quick", None):
            self._agents_quick = agents.statuses(check=False)
        return self._agents_quick

    def _refresh_agents(self) -> None:
        if getattr(self, "_agents_busy", False):
            return
        self._agents_busy = True

        def work():
            try:
                data = agents.statuses()
                self._agents, self._agents_at = data, time.monotonic()
                try:
                    self._agents_cache_file().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                except OSError:
                    pass
                self.agentsChanged.emit(json.dumps(data, ensure_ascii=False))
            finally:
                self._agents_busy = False

        threading.Thread(target=work, daemon=True).start()

    # -- 만들기·실행
    @Slot(str, result=str)
    @safe
    def createRun(self, payload):
        v = json.loads(payload)
        if not v.get("target_root"):
            # 폴더를 안 고르면 새 프로젝트로 — 문서 폴더 아래 모드별 새 폴더 (툴킷 홈은 에이전트가 쓸 수 없는 곳이라 안 됨).
            # 기존 코드에 넣으려면 그 저장소나, 저장소 여럿을 모은 상위 폴더를 고른다.
            slug = re.sub(r"[^\w가-힣-]+", "-", (v.get("request") or v.get("feature") or v.get("mode") or "work").strip()[:30]).strip("-") or "work"
            docs_dir = Path.home() / "Documents"
            sub = {"plan": "기획", "request": "만들기", "reference": "레퍼런스", "free": "자유 대화"}.get(v.get("mode"), "작업")
            base = (docs_dir if docs_dir.is_dir() else Path.home()) / "beom-workbench" / sub
            v["target_root"] = str(base / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}")
        if not v.get("target_root"):
            raise ToolError("invalid_request", "작업할 폴더를 정해 주세요")
        if v["mode"] in ("plan", "free") and not (v.get("request") or "").strip() and not v.get("attachments"):
            raise ToolError("invalid_request", "무엇을 할지 적어 주세요")
        if v["mode"] == "request" and not v.get("request", "").strip() and not v.get("attachments") and not v.get("urls"):
            raise ToolError("invalid_request", "만들 기능을 설명하거나 기획서를 첨부해 주세요")
        if v["mode"] == "reference" and not v.get("source", "").strip():
            raise ToolError("invalid_request", "원본 저장소를 입력해 주세요")
        if v.get("auth") == "api_key":
            names = agents.get(v.get("agent", "claude")).api_key_env
            if names and not any(os.environ.get(n) for n in names):
                raise ToolError("not_ready", f"API 키 인증을 골랐지만 환경 변수({' 또는 '.join(names)})가 없습니다",
                                recovery="키를 환경 변수로 넣고 앱을 다시 열거나, 인증을 '내 로그인'으로 두세요")
        threading.Thread(target=self._prepare, args=(v,), daemon=True).start()
        try:  # 다음 새 작업은 이번 선택으로 시작 (Claude 고정이 아니라 쓰던 에이전트)
            from beom_workbench.core import storage
            storage.save_prefs(self.settings, {"defaults": {k: v.get(k) for k in (
                "agent", "model", "effort", "policy", "max_rounds", "plan_approval", "confirm_at", "auth") if k in v}})
        except Exception:  # noqa: BLE001 — 기본값 저장 실패가 실행을 막지 않게
            pass
        return {"preparing": True}

    def _prepare(self, v: dict) -> None:
        try:
            # 첨부는 에이전트가 실제로 일할 폴더에 — 바탕 화면 같은 공용 폴더면 그 안의 작업 폴더 (create 와 같은 규칙)
            v = {**v, "target_root": runner.work_folder(v["target_root"], v.get("name"), v["mode"])}
            request, notes = self._attach(v)
            v["request"] = request
            context = None
            if v.get("context_dir"):
                opened = project.open_repo(self.settings, v["context_dir"])
                conn, proj = project.load(self.settings, opened["project"]["id"])
                if not proj.get("inventory_at"):
                    inventory.build(conn, self.settings, proj, False)
                context = proj["id"]
            if v["mode"] == "reference":
                # 원본 분석을 먼저 — 실패하면 실행도 작업 브랜치도 만들지 않는다 (대상 저장소를 새 브랜치·stash 에 남기지 않게)
                opened = project.open_repo(self.settings, v["source"], None, v.get("name") or None)
                conn, proj = project.load(self.settings, opened["project"]["id"])
                if not proj.get("inventory_at"):
                    inventory.build(conn, self.settings, proj, False)
            run = runner.create(self.settings, mode=v["mode"], target_root=v["target_root"], request=v.get("request", ""),
                                name=v.get("name") or None, source=v.get("source"), feature=v.get("feature"),
                                source_run=v.get("source_run"), context_project=context, max_rounds=int(v.get("max_rounds") or 0),
                                model=v.get("model") or None, auth=v.get("auth", "subscription"), policy=v.get("policy"),
                                agent=v.get("agent", "claude"), plan_approval=bool(v.get("plan_approval", True)),
                                effort=v.get("effort") or None, confirm_at=v.get("confirm_at") or "start",
                                new_branch=v.get("new_branch", True) is not False, sandbox=v.get("sandbox", True) is not False)
            for note in notes:
                inbox.log(self.settings, run["id"], "note", text=note, tone="warn")
            self.created.emit(json.dumps({"run_id": run["id"], "start": bool(v.get("start", True))}))
        except Exception as e:  # noqa: BLE001
            msg = getattr(e, "message", None) or f"{type(e).__name__}: {e}"
            if getattr(e, "recovery", None):
                msg += f" — {e.recovery}"
            self.created.emit(json.dumps({"error": msg}, ensure_ascii=False))

    def _attach(self, v: dict, *, follow_up: bool = False) -> tuple[str, list[str]]:
        """첨부한 기획서·문서·링크를 대상 폴더의 .beom/brief/ 에 두고 요청에 '먼저 읽으라'고 적는다 (에이전트 샌드박스 안).

        docx·pptx·xlsx·hwpx 는 글로 바꾼 .md 를 함께 둔다. 링크는 공개 페이지면 글로 저장하고,
        로그인이 필요한 링크는 화면이 먼저 앱 브라우저로 로그인을 요청해 글로 바꿔 둔다 — 그래도 남은 링크는 에이전트가 웹으로 읽게 한다.
        """
        from beom_workbench.core import docs

        files = [Path(f) for f in v.get("attachments") or [] if f and not re.match(r"https?://", str(f))]
        urls = [str(f).strip() for f in (v.get("attachments") or []) + (v.get("urls") or []) if re.match(r"https?://", str(f))]
        request = (v.get("request") or "").strip()
        notes: list[str] = []
        if not files and not urls:
            return request, notes
        brief = Path(v["target_root"]) / ".beom" / "brief"
        brief.mkdir(parents=True, exist_ok=True)
        names, links = [], []
        for f in files:  # 모두 확인한 뒤에 복사한다 — 하나가 잘못돼도 앞의 파일만 반쯤 복사된 채 남지 않게
            if not f.is_file():
                raise ToolError("not_found", f"첨부 파일이 없습니다: {f}")
            if f.stat().st_size > 20 * 1024 * 1024:
                raise ToolError("invalid_request", f"첨부 파일이 너무 큽니다 (20MB 까지): {f.name}")
            docs.check_attachable(f)  # .hwp 등 읽을 수 없는 형식은 복사하기 전에 거절
        for f in files:
            if docs.is_link_package(f) and not f.resolve().is_relative_to(brief.resolve()):  # 로그인 창으로 가져온 문서 — 그림·첨부와 함께
                dest = brief / f"link-{f.parent.name}"
                shutil.copytree(f.parent, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".link.json"))
                names.append(f".beom/brief/{dest.name}/{f.name} (로그인해 가져온 문서 · 그림·첨부는 같은 폴더의 files/)")
                continue
            if f.resolve().is_relative_to(brief.resolve()):  # 같은 폴더의 기획 패키지 — 그 자리에 있는 파일을 그대로 읽게
                names.append(".beom/brief/" + f.resolve().relative_to(brief.resolve()).as_posix())
                continue
            dest = _unique_dest(brief, f)  # 같은 이름의 다른 파일(예: 두 번째 screenshot.png)이 앞의 첨부를 덮지 않게
            shutil.copy2(f, dest)
            text = docs.to_text(f)
            if text:
                md = brief / f"{dest.name}.md"  # spec.docx → spec.docx.md (spec.md 나 기획 결과 기획서.md 와 겹치지 않게)
                md.write_text(f"<!-- {dest.name} 에서 뽑은 글 -->\n\n{text}", encoding="utf-8")
                names.append(f".beom/brief/{md.name} (원본 {dest.name})")
            else:
                names.append(f".beom/brief/{dest.name}")
        for i, url in enumerate(dict.fromkeys(urls), 1):
            n = i
            while (brief / f"link-{n}").exists():
                n += 1
            got = docs.fetch_link(url, brief / f"link-{n}")  # 본문(표·목록 구조 그대로) + 그림
            if got.get("ok"):
                md = Path(got["path"])
                names.append(f".beom/brief/{md.parent.name}/{md.name} (출처 {url})")
            else:
                links.append(url)
                why = "로그인이 필요한 페이지" if got.get("needs_login") else got.get("error", "가져오지 못함")
                notes.append(f"{url} — {why}라 내용을 가져오지 못했습니다. 에이전트가 웹으로 읽으려 하면 권한을 묻습니다 "
                                          "(새 작업 화면에서 링크의 '로그인'을 누르면 앱 브라우저로 로그인해 내용을 가져옵니다).")
        if follow_up:  # 진행 중·끝난 실행에 보내는 메시지의 첨부 — 기준을 새로 세우는 게 아니라 이 메시지의 근거
            head = request or "첨부한 파일을 보고 반영한다."
            out = head + "\n\n[이 메시지의 첨부 — 먼저 읽고 반영한다]\n" + "\n".join(f"- {n}" for n in names)
        else:
            head = request or "첨부한 기획서대로 만든다."
            out = head + "\n\n[첨부 문서 — 먼저 모두 읽고 요구사항·합격 기준에 반영한다. 문서와 요청이 다르면 묻는다]\n" + "\n".join(f"- {n}" for n in names)
        if links:
            out += "\n[링크 — 내용을 미리 받지 못했다. 웹으로 읽을 수 있으면 읽고, 못 읽으면 사용자에게 내용을 요청한다]\n" + "\n".join(f"- {u}" for u in links)
        return out, notes

    @Slot(str, result=str)
    @safe
    def screenshot(self, path):
        """검증이 남긴 스크린샷을 화면에 — 툴킷 홈 안의 png 만 (data: 주소로)."""
        import base64

        p = Path(path).resolve()
        if p.suffix.lower() != ".png" or not inbox._inside(str(p), self.settings.home) or not p.is_file():
            raise ToolError("invalid_request", "보여 줄 수 없는 파일")
        if p.stat().st_size > 8 * 1024 * 1024:
            raise ToolError("invalid_request", "스크린샷이 너무 큽니다")
        return {"url": "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()}

    @Slot(str, result=str)
    @safe
    def briefFiles(self, run_id):
        """기획 패키지의 문서 목록·와이어프레임 스크린샷·점검 결과."""
        from beom_workbench.core import brief

        run = inbox.load_run(self.settings, run_id)
        return {**brief.files(run["target_root"]), "check": brief.check(run["target_root"])}

    @Slot(str, str, result=str)
    @safe
    def briefShot(self, run_id, name):
        """기획 와이어프레임 스크린샷 — 그 실행의 .beom/brief/shots 안 png 만."""
        import base64

        from beom_workbench.core import brief

        run = inbox.load_run(self.settings, run_id)
        d = brief.folder(run["target_root"]) / "shots"
        f = d / Path(name).name
        if f.suffix.lower() != ".png" or not f.is_file() or f.is_symlink() or f.stat().st_size > 8 * 1024 * 1024:
            raise ToolError("invalid_request", "보여 줄 수 없는 파일")
        return {"url": "data:image/png;base64," + base64.b64encode(f.read_bytes()).decode()}

    @Slot(str, result=str)
    @safe
    def briefScreens(self, run_id):
        """화면 설계 프로토타입 — 그 실행의 .beom/brief/wireframes 안 HTML 들 (글자 그대로. 앱은 격리된 틀 안에서만 띄운다)."""
        from beom_workbench.core import brief

        run = inbox.load_run(self.settings, run_id)
        d = brief.folder(run["target_root"]) / "wireframes"
        out = []
        for f in sorted(d.glob("*.htm*")) if d.is_dir() else []:
            if f.is_file() and not f.is_symlink() and f.stat().st_size <= 3 * 1024 * 1024:
                out.append({"name": f.name, "html": f.read_text(encoding="utf-8", errors="replace")})
        return out

    @Slot(str, str, str, result=str)
    @safe
    def exportBrief(self, run_id, html, fmt):
        return self._export_brief(run_id, html, fmt, "")

    @Slot(str, str, str, str, result=str)
    @safe
    def exportBriefWith(self, run_id, html, fmt, data_json):
        """data_json: 화면 설계 프로토타입 자료 — 우리 보기 스크립트(proto.js)와 함께 파일에 넣어 눌러 볼 수 있게 한다."""
        return self._export_brief(run_id, html, fmt, data_json)

    def _export_brief(self, run_id, html, fmt, data_json):
        """화면에서 만든 기획 패키지 HTML(그림 포함 한 파일)을 .beom/brief/export/ 에 저장한다. pdf 면 그 파일을 브라우저 엔진으로
        인쇄해 PDF 를 만들고 끝나면 exportDone 으로 알린다 (인쇄는 비동기).
        화면에서 온 HTML 에는 스크립트를 받지 않는다 — 스크립트는 앱 안의 proto.js 만, 자료는 JSON 블록으로 넣는다."""
        import re as _re

        from beom_workbench.core import brief

        run = inbox.load_run(self.settings, run_id)
        out = brief.folder(run["target_root"]) / "export"
        out.mkdir(parents=True, exist_ok=True)
        if len(html) + len(data_json or "") > 80 * 1024 * 1024 or "<script" in html.lower():
            raise ToolError("invalid_request", "내보낼 수 없는 내용입니다")
        data = json.loads(data_json) if data_json else {}
        web = Path(__file__).parent / "web"
        runtime = (web / "proto.js").read_text(encoding="utf-8")
        if data.get("proto"):  # 내보낸 파일에서는 화면을 srcdoc 으로 띄우므로 화면 안 스크립트를 함께 싣는다
            data["proto"]["inject"] = (web / "proto-frame.js").read_text(encoding="utf-8")
            for sc in (data["proto"].get("screens") or {}).values():
                sc.pop("src", None)
        blob = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
        tail = (f'<script id="beom-data" type="application/json">{blob}</script>'
                f"<script>{runtime}</script><script>OssExport()</script>")
        html = html.replace("</body>", tail + "</body>", 1) if "</body>" in html else html + tail
        stem = _re.sub(r'[\\/:*?"<>|]+', " ", (run.get("name") or "기획")).strip()[:60] or "기획"
        f = out / f"{stem} - 기획 패키지.html"
        f.write_text(html, encoding="utf-8")
        res = {"html": str(f), "folder": str(out)}
        if fmt == "pdf":
            self._print_pdf(f, f.with_suffix(".pdf"))
            res["pdf_pending"] = True
        return res

    def _print_pdf(self, src: Path, dst: Path) -> None:
        from PySide6.QtCore import QMarginsF, QUrl
        from PySide6.QtGui import QPageLayout, QPageSize
        from PySide6.QtWebEngineCore import QWebEnginePage

        page = QWebEnginePage(self)
        self._pdf_jobs = getattr(self, "_pdf_jobs", [])
        self._pdf_jobs.append(page)  # 인쇄가 끝날 때까지 붙잡아 둔다

        def done(path, ok):
            self.exportDone.emit(json.dumps({"ok": bool(ok), "path": path, "folder": str(dst.parent)}, ensure_ascii=False))
            self._pdf_jobs.remove(page)
            page.deleteLater()

        def loaded(ok):
            if not ok:
                done(str(dst), False)
                return
            layout = QPageLayout(QPageSize(QPageSize.A4), QPageLayout.Portrait, QMarginsF(13, 14, 13, 14), QPageLayout.Millimeter)
            page.printToPdf(str(dst), layout)

        page.pdfPrintingFinished.connect(done)
        page.loadFinished.connect(loaded)
        page.load(QUrl.fromLocalFile(str(src)))

    @Slot(str, str, result=str)
    @safe
    def runImage(self, run_id, rel):
        """계획 미리보기 그림 — 그 실행의 대상 폴더 .beom/ 안 png 만."""
        import base64

        run = inbox.load_run(self.settings, run_id)
        base = (Path(run["target_root"]) / ".beom").resolve()
        f = (Path(run["target_root"]) / rel).resolve()
        if f.suffix.lower() != ".png" or not f.is_file() or not inbox._inside(str(f), base) or f.stat().st_size > 12 * 1024 * 1024:
            raise ToolError("invalid_request", "보여 줄 수 없는 파일")
        return {"url": "data:image/png;base64," + base64.b64encode(f.read_bytes()).decode()}

    @Slot(str, str, result=str)
    @safe
    def readBriefDoc(self, run_id, name):
        from beom_workbench.core import brief

        run = inbox.load_run(self.settings, run_id)
        d = brief.folder(run["target_root"])
        f = d / Path(name).name
        if f.suffix.lower() != ".md" or not f.is_file() or f.is_symlink() or not inbox._inside(str(f.resolve()), d.resolve()):
            raise ToolError("invalid_request", "기획 폴더 안의 문서만 열 수 있습니다")
        return {"path": str(f), "text": f.read_text(encoding="utf-8", errors="replace")[:400_000]}

    @Slot(str, result=str)
    @safe
    def readBrief(self, run_id):
        """기획 모드의 결과 문서 (.beom/brief/기획서.md) — 없으면 가장 최근에 바뀐 .md."""
        run = inbox.load_run(self.settings, run_id)
        brief = Path(run["target_root"]) / ".beom" / "brief"
        main = brief / "기획서.md"
        if not main.is_file():
            mds = sorted(brief.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True) if brief.is_dir() else []
            main = mds[0] if mds else None
        if not main:
            return {"path": None, "text": ""}
        # 에이전트가 만든 파일이다 — 심볼릭 링크나 폴더 밖을 가리키면 읽지 않는다
        if main.is_symlink() or not inbox._inside(str(main.resolve()), brief.resolve()):
            raise ToolError("invalid_request", "기획서 파일이 기획 폴더 밖을 가리킵니다")
        return {"path": str(main), "text": main.read_text(encoding="utf-8", errors="replace")[:400_000]}

    @Slot(result=str)
    @safe
    def pickFiles(self):
        files, _ = QFileDialog.getOpenFileNames(None, "기획서·문서 첨부", str(Path.home()),
                                                "문서 (*.md *.txt *.pdf *.docx *.pptx *.xlsx *.hwpx *.json *.yaml *.yml *.csv *.png *.jpg);;모든 파일 (*)")
        return self._screen_files(files)

    def _screen_files(self, files: list[str]) -> dict:
        """고른 파일 중 첨부할 수 없는 것(.hwp 등)은 빼고 이유를 함께 돌려준다."""
        from beom_workbench.core import docs

        ok, rejected = [], []
        for f in files:
            p = Path(f)
            if p.is_dir():  # 끌어다 놓은 폴더
                rejected.append(f"폴더는 첨부할 수 없습니다 — 파일을 골라 주세요: {p.name}")
                continue
            if not p.is_file():
                rejected.append(f"첨부 파일이 없습니다: {p.name}")
                continue
            if p.stat().st_size > 20 * 1024 * 1024:
                rejected.append(f"첨부 파일이 너무 큽니다 (20MB 까지): {p.name}")
                continue
            try:
                docs.check_attachable(p)
                ok.append(f)
            except ToolError as e:
                rejected.append(e.message)
        return {"files": ok, "rejected": rejected}

    @Slot(str, result=str)
    @safe
    def checkFiles(self, raw):
        """끌어다 놓은 파일 확인 — pickFiles 와 같은 규칙."""
        return self._screen_files(json.loads(raw or "[]"))

    def _busy(self, run_id) -> bool:
        """이 창의 스레드든 다른 곳(CLI·다른 창)이든 지금 이 실행을 진행 중인가."""
        t = self.threads.get(run_id)
        return bool(t and t.is_alive()) or runner.lock_owner(self.settings, run_id) is not None

    @Slot(str, result=str)
    @safe
    def start(self, run_id):
        if self._busy(run_id):
            return {"running": True}
        tam = inbox.load_run(self.settings, run_id).get("tampered")
        if tam:
            raise ToolError("denied", "앱 밖에서 바뀐 결정이 있어 멈춘 작업입니다 — 카드의 '확인했음'을 누른 뒤 이어 가세요")

        def more_rounds(x):  # 사용자가 이어서 진행을 눌렀다 — 라운드를 다 썼으면 몇 개 더 준다 (0 = 제한 없음)
            if int(x.get("round") or 0) >= rounds.get(self.settings)["hard_rounds"]:
                x["round"] = 0  # 안전 상한에 걸렸던 실행 — 사람이 이어 가기로 했다
            if x.get("max_rounds") and int(x.get("round") or 0) >= int(x["max_rounds"]):
                x["max_rounds"] = int(x.get("round") or 0) + 4
            x["cost_base"] = x.get("cost_usd") or 0  # 비용 상한은 이어서 누른 때부터 다시 센다

        inbox.update_run(self.settings, run_id, more_rounds)
        r = runner.Runner(self.settings, run_id)
        # 준비 확인은 에이전트 프로그램을 띄우는 일이라(화면 스레드) 몇 분 안에 확인한 '준비됨'이 있으면 그대로 쓴다
        cached = next((a for a in (getattr(self, "_agents", None) or []) if a.get("id") == r.agent.id), None)
        fresh = cached and cached.get("ready") and time.monotonic() - getattr(self, "_agents_at", 0) < AGENT_CHECK_SEC
        st = cached if fresh else r.agent.status()
        if not st.get("ready") and r.agent.id != "gemini":
            raise ToolError("not_ready", st.get("hint") or f"{r.agent.name} 준비가 필요합니다")
        self.runners[run_id] = r

        def work():
            try:
                r.run()
            except ToolError as e:
                if e.code == "conflict":  # 다른 창·CLI 가 이미 진행 중 — 그 실행의 상태는 건드리지 않는다
                    inbox.log(self.settings, run_id, "note", text=e.message)
                    return
                inbox.log(self.settings, run_id, "error", message=e.message, trace=traceback.format_exc()[-1500:])
                inbox.update_run(self.settings, run_id, lambda x: x.update(status="error"))
            except Exception as e:  # noqa: BLE001
                inbox.log(self.settings, run_id, "error", message=f"{type(e).__name__}: {getattr(e, 'message', e)}",
                          trace=traceback.format_exc()[-1500:])
                inbox.update_run(self.settings, run_id, lambda x: x.update(status="error"))

        t = threading.Thread(target=work, daemon=True)
        self.threads[run_id] = t
        t.start()
        return {"running": True}

    def _editable(self, run_id) -> dict:
        run = inbox.load_run(self.settings, run_id)
        if self._busy(run_id) and run.get("status") == "running":
            raise ToolError("invalid_request", "에이전트가 작업 중입니다. 멈춘 뒤 고치거나, 입력창으로 고칠 점을 보내 주세요")
        return run

    def _tell_agent(self, run_id, text):
        inbox.update_run(self.settings, run_id, lambda r: r.setdefault("messages", []).append(
            {"text": text[:4000], "at": view.now(), "delivered": False, "from": "edit"}))

    @Slot(str, result=str)
    @safe
    def specForEdit(self, run_id):
        """요구사항·합격 케이스를 편집 화면에 채울 값 — 원장의 요구사항과 마지막 캡처의 케이스(자동 생성분 제외)."""
        run = inbox.load_run(self.settings, run_id)
        conn, _ = project.load(self.settings, run["project_id"])
        from beom_workbench.core import make
        reqs = [{"id": r["id"], "text": r["text"]} for r in make.requirements(conn, run["port_id"])]
        cap = port._last_check(conn, run["port_id"], "capture")
        cases = []
        if cap and Path(cap["detail"].get("golden", "")).is_file():
            g = json.loads(Path(cap["detail"]["golden"]).read_text(encoding="utf-8"))
            for c in g.get("cases") or []:
                if c.get("generated"):
                    continue
                cases.append({k: v for k, v in c.items() if k not in ("new", "generated", "from")})
        return {"requirements": reqs, "cases": cases}

    @Slot(str, str, result=str)
    @safe
    def saveSpec(self, run_id, payload):
        """사용자가 고친 요구사항·합격 케이스를 저장한다 (원장에 사용자 작성으로 기록). 승인은 따로 — 바뀐 내용으로 다시 승인."""
        run = self._editable(run_id)
        v = json.loads(payload)
        conn, proj = project.load(self.settings, run["project_id"])
        from beom_workbench.core import make
        who = f"user:{getpass.getuser()}"
        notes = []
        if v.get("requirements") is not None:
            before = {r["id"]: r["text"] for r in make.requirements(conn, run["port_id"])}
            make.set_requirements(conn, run["port_id"], v["requirements"], actor=who)
            after = {r["id"]: r["text"] for r in make.requirements(conn, run["port_id"])}
            changed = [k for k in after if before.get(k) != after[k]] + [f"{k}(삭제)" for k in before if k not in after]
            if changed:
                notes.append("요구사항 " + ", ".join(changed))
        if v.get("cases") is not None:
            if not v["cases"]:
                raise ToolError("invalid_request", "합격 케이스가 비어 있습니다")
            path = Path(run["target_root"]) / ".beom" / "cases.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"cases": v["cases"]}, ensure_ascii=False, indent=2), encoding="utf-8")
            port.capture(conn, proj, self.settings, run["port_id"], str(path))
            notes.append(f"합격 케이스 {len(v['cases'])}개")
        if notes:
            inbox.log(self.settings, run_id, "user_edit", what=" · ".join(notes))
            self._tell_agent(run_id, "사용자가 앱에서 직접 고쳤다 — " + " · ".join(notes)
                             + ". 원장과 .beom/cases.json 의 지금 내용을 기준으로 진행한다 (다시 바꾸지 않는다).")
        return {"saved": notes, "review_sig": view.detail(self.settings, run_id).get("review_sig")}

    @Slot(str, str, result=str)
    @safe
    def savePlan(self, run_id, payload):
        """사용자가 고친 계획을 저장한다. 승인은 따로 — 저장하면 승인 전 상태가 된다."""
        self._editable(run_id)
        v = json.loads(payload)
        steps = [{"title": str(s.get("title", "")).strip()[:200], "files": [str(f).strip() for f in s.get("files") or [] if str(f).strip()][:20],
                  "commands": [str(c).strip() for c in s.get("commands") or [] if str(c).strip()][:10],
                  "requirements": s.get("requirements") or [], "status": s.get("status") or "todo"}
                 for s in v.get("steps") or [] if str(s.get("title", "")).strip()]
        if not steps:
            raise ToolError("invalid_request", "단계가 비어 있습니다")

        def put(r):
            old = r.get("plan") or {}
            hist = (old.get("history") or []) + ([{"steps": old["steps"], "summary": old.get("summary")}] if old.get("steps") else [])
            r["plan"] = {"steps": steps, "summary": str(v.get("summary") or old.get("summary") or "").strip()[:1000],
                         "approved": False, "history": hist[-5:], "at": view.now(), "edited_by": getpass.getuser(),
                         **({"design": str(v["design"] if v.get("design") is not None else old.get("design") or "")[:6000]}
                            if (v.get("design") is not None or old.get("design")) else {}),
                         **({"decisions": old["decisions"]} if old.get("decisions") else {})}

        inbox.update_run(self.settings, run_id, put)
        inbox.log(self.settings, run_id, "user_edit", what=f"계획 {len(steps)}단계")
        self._tell_agent(run_id, f"사용자가 앱에서 계획을 직접 고쳤다 ({len(steps)}단계). 승인되면 이 계획대로 진행한다.")
        return {"saved": True, "plan_sig": view.detail(self.settings, run_id).get("plan_sig")}

    # -- 실행 목록 정리
    @Slot(str, str, result=str)
    @safe
    def renameRun(self, run_id, name):
        name = name.strip()[:60]
        if not name:
            raise ToolError("invalid_request", "이름이 비어 있습니다")
        inbox.update_run(self.settings, run_id, lambda r: r.update(name=name))
        return {"name": name}

    @Slot(str, bool, result=str)
    @safe
    def archiveRun(self, run_id, archived):
        if archived and self._busy(run_id):
            raise ToolError("invalid_request", "진행 중인 실행은 숨길 수 없습니다. 먼저 멈춰 주세요")
        inbox.update_run(self.settings, run_id, lambda r: r.update(archived=bool(archived)))
        return {"archived": bool(archived)}

    # -- 바로가기 (시작 메뉴·바탕화면)
    def _prefs(self) -> dict:
        from beom_workbench.core import storage
        return storage.load_prefs(self.settings)

    @Slot(result=str)
    @safe
    def shortcutStatus(self):
        from beom_workbench.core import shortcut
        return {**shortcut.status(), "dismissed": bool(self._prefs().get("shortcut_dismissed"))}

    @Slot(str, result=str)
    @safe
    def installShortcut(self, where):
        from beom_workbench.app.main import write_icon_files
        from beom_workbench.core import shortcut
        locs = tuple(w for w in where.split(",") if w in shortcut.LOCATIONS) or shortcut.LOCATIONS
        return shortcut.create(self.settings, locs, icon=write_icon_files(self.settings.home))

    @Slot(result=str)
    @safe
    def dismissShortcut(self):
        from beom_workbench.core import storage
        storage.save_prefs(self.settings, {"shortcut_dismissed": True})
        return {"dismissed": True}

    # -- 데이터 (저장 위치·크기·휴지통·정리)
    dataReady = Signal(str)

    @Slot(result=str)
    @safe
    def dataReport(self):
        """크기 계산은 몇 초 걸릴 수 있어 뒤에서 하고 dataReady 로 보낸다. 바로는 크기 없이 돌려준다 (계산 중이면 또 시작하지 않는다)."""
        from beom_workbench.core import storage

        def work():
            try:
                self.dataReady.emit(json.dumps({"ok": True, "data": storage.report(self.settings)}, ensure_ascii=False))
            except Exception as e:  # noqa: BLE001
                self.dataReady.emit(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            finally:
                self._data_busy = False

        if not getattr(self, "_data_busy", False):
            self._data_busy = True
            threading.Thread(target=work, daemon=True).start()
        return storage.report(self.settings, sizes=False)

    @Slot(str, result=str)
    @safe
    def trashRun(self, run_id):
        from beom_workbench.core import storage
        if self._busy(run_id):
            raise ToolError("invalid_request", "진행 중인 실행은 지울 수 없습니다. 먼저 멈춰 주세요")
        return storage.trash_run(self.settings, run_id)

    @Slot(str, result=str)
    @safe
    def restoreRun(self, name):
        from beom_workbench.core import storage
        return storage.restore_run(self.settings, name)

    @Slot(result=str)
    @safe
    def emptyTrash(self):
        from beom_workbench.core import storage
        return storage.empty_trash(self.settings)

    @Slot(result=str)
    @safe
    def clearBrowser(self):
        """문서 가져오기 창의 로그인(쿠키)·캐시를 지운다 — 이 창을 연 적이 있으면 열린 프로필로, 아니면 폴더째."""
        from beom_workbench.app import linkbrowser
        from beom_workbench.core import storage
        prof = linkbrowser._PROFILE.get("p")
        if prof is not None:
            prof.cookieStore().deleteAllCookies()
            prof.clearHttpCache()
            prof.clearAllVisitedLinks()
            return {"cleared": True}
        return storage.clear_browser(self.settings)

    @Slot(str, result=str)
    @safe
    def deleteProject(self, project_id):
        from beom_workbench.core import storage
        return {"deleted": storage.delete_project(self.settings, project_id)}

    @Slot(str, result=str)
    @safe
    def setDataPrefs(self, payload):
        from beom_workbench.core import storage
        return storage.save_prefs(self.settings, json.loads(payload))

    @Slot(result=str)
    @safe
    def roundRules(self):
        return rounds.describe(self.settings)

    @Slot(str, result=str)
    @safe
    def setRoundRules(self, payload):
        """라운드 규칙 — 모든 실행에 쓰이고, 도는 중인 실행도 다음 판단부터 따른다. null 은 기본값으로."""
        from beom_workbench.core import storage

        storage.save_prefs(self.settings, {"loop": json.loads(payload)})
        return rounds.describe(self.settings)

    @Slot(str, str, result=str)
    @safe
    def setRunOptions(self, run_id, payload):
        """사용자가 실행 중에 바꾸는 설정 — 모델(다음 라운드부터), 권한 규칙(바로), 최대 라운드.
        에이전트는 규칙을 엄격하게만 바꿀 수 있지만 사람은 어느 쪽으로든 바꿀 수 있다."""
        v = json.loads(payload)

        def put(r):
            if "model" in v:
                r["model"] = (v["model"] or "").strip() or None
            if "effort" in v:
                r["effort"] = (v["effort"] or "").strip() or None
            if isinstance(v.get("policy"), dict):
                integrity.record(run_id, "policy")
                r["policy"] = {k: (v["policy"].get(k) if v["policy"].get(k) in inbox.DECISIONS else r["policy"].get(k, "ask"))
                               for k in inbox.KINDS}
            if "max_rounds" in v:
                n = int(v["max_rounds"] or 0)
                r["max_rounds"] = 0 if n <= 0 else max(int(r.get("round") or 0) + 1, min(500, n))
            if "plan_approval" in v:
                r["plan_approval"] = bool(v["plan_approval"])
            if "sandbox" in v:  # 격리 실행 끄기·켜기 — 다음 명령부터
                r["sandbox"] = bool(v["sandbox"])
            if v.get("confirm_at") in ("start", "end"):
                r["confirm_at"] = v["confirm_at"]

        run = inbox.update_run(self.settings, run_id, put)
        inbox.log(self.settings, run_id, "options", model=run.get("model"), mode=inbox.mode_of(run["policy"]),
                  max_rounds=run.get("max_rounds"))
        return {"model": run.get("model"), "effort": run.get("effort"), "policy": run["policy"], "max_rounds": run.get("max_rounds"),
                "sandbox": run.get("sandbox", True),
                "plan_approval": run.get("plan_approval", True), "confirm_at": run.get("confirm_at", "start")}

    @Slot(str, result=str)
    @safe
    def stop(self, run_id):
        if run_id in self.runners:
            self.runners[run_id].stop()
        if runner.lock_owner(self.settings, run_id) is not None:
            runner.request_stop(self.settings, run_id)  # CLI·다른 창에서 도는 실행도 멈춘다
        return {"stopping": True}

    # -- 사람의 결정 (앱 프로세스 안에서만 기록된다)
    @Slot(str, str, result=str)
    @safe
    def approveReviews(self, run_id, shown_sig):
        run = inbox.load_run(self.settings, run_id)
        conn, _ = project.load(self.settings, run["project_id"])
        reviews = port.pending_reviews(conn, run["port_id"])
        if view.sig(reviews) != shown_sig:  # 화면을 본 뒤 에이전트가 내용을 바꿨다 — 본 것만 승인한다
            raise ToolError("conflict", "확인하는 사이에 내용이 바뀌었습니다. 새 내용을 보고 다시 승인해 주세요")
        integrity.record(run_id, "reviews")
        guard.approve(conn, run["port_id"], reviews, channel="desktop-app", approved_by=getpass.getuser())
        inbox.log(self.settings, run_id, "approved", items=[r["item_id"] for r in reviews])
        return {"approved": len(reviews)}

    @Slot(str, str, result=str)
    @safe
    def ackLicense(self, run_id, shown_warning):
        """레퍼런스: 원본 라이선스 경고를 사람이 보고 '옮겨도 됨'으로 확인 — 본 경고 문구 그대로일 때만."""
        run = inbox.load_run(self.settings, run_id)
        conn, _ = project.load(self.settings, run["project_id"])
        st = port.status_(conn, run["port_id"], brief=True)
        if ((st.get("license") or {}).get("warning") or "") != shown_warning:
            raise ToolError("conflict", "확인하는 사이에 라이선스 경고가 바뀌었습니다. 새 내용을 보고 다시 확인해 주세요")
        integrity.record(run_id, "reviews")
        out = port.ack_license(conn, run["port_id"], channel="desktop-app", approved_by=getpass.getuser())
        inbox.log(self.settings, run_id, "license_ack", warning=shown_warning[:500])
        return out

    @Slot(str, str, result=str)
    @safe
    def approvePlan(self, run_id, shown_sig):
        return self._approve_plan(run_id, shown_sig, {})

    @Slot(str, str, str, result=str)
    @safe
    def approvePlanWith(self, run_id, shown_sig, changes):
        """사전 설계 결정 중 몇 개를 대안으로 바꿔 승인 — {주제: 고른 대안}. 바꾼 내용은 다음 라운드 지시로 간다."""
        return self._approve_plan(run_id, shown_sig, json.loads(changes or "{}"))

    def _approve_plan(self, run_id, shown_sig, changes: dict):
        changed = []

        def ok(r):
            plan = r.get("plan") or {}
            if not plan.get("steps"):
                raise ToolError("invalid_request", "승인할 계획이 없습니다")
            if view.sig(view.plan_core(plan)) != shown_sig:
                raise ToolError("conflict", "확인하는 사이에 계획이 바뀌었습니다. 새 계획을 보고 다시 승인해 주세요")
            for d in plan.get("decisions") or []:
                pick = changes.get(d["topic"])
                if pick and pick != d["choice"] and pick in (d.get("options") or []):
                    changed.append(f"{d['topic']}: {d['choice']} → {pick}")
                    d["options"] = [d["choice"]] + [o for o in d["options"] if o != pick]
                    d["choice"], d["changed_by_user"] = pick, True
            if changed:
                r.setdefault("messages", []).append({
                    "text": "계획을 승인하면서 사전 설계 결정을 바꿨다 — 요구사항·합격 케이스·계획·구현을 이 결정에 맞춘다. "
                            "이번 라운드에 고치는 요구사항·케이스는 사용자 결정의 결과라 툴킷이 자동 승인한다 (다시 확인을 기다리지 않는다):\n"
                            + "\n".join(f"- {c}" for c in changed), "at": view.now(), "delivered": False})
                r["auto_approve_round"] = int(r.get("round") or 0) + 1
            plan["approved"] = True
            plan["approved_by"] = getpass.getuser()
            plan["approved_at"] = view.now()

        integrity.record(run_id, "plan")
        inbox.update_run(self.settings, run_id, ok)
        inbox.log(self.settings, run_id, "plan_approved", **({"changed": changed} if changed else {}))
        return {"approved": True, "changed": changed}

    @Slot(str, str, str, result=str)
    @safe
    def answer(self, run_id, request_id, payload):
        data = json.loads(payload)
        if data.get("always") and data.get("allow"):
            integrity.record(run_id, "policy")  # '항상 허용' — 도구 서버가 규칙을 허용으로 바꾼다
        inbox.answer(self.settings, run_id, request_id, data)
        return {"answered": True}

    @Slot(str, str, str, result=str)
    @safe
    def sendMessage(self, run_id, text, atts=""):
        text = text.strip()[:4000]
        files = [str(f) for f in json.loads(atts or "[]") if str(f).strip()]
        if not text and not files:
            raise ToolError("invalid_request", "내용이 비어 있습니다")
        notes: list[str] = []
        if files:  # 고칠 점을 보여 주는 스크린샷·문서 — 처음 첨부와 같은 규칙으로 대상 폴더의 .beom/brief/ 에 둔다
            target = inbox.load_run(self.settings, run_id).get("target_root")
            if not target:
                raise ToolError("not_found", "실행을 찾을 수 없습니다")
            text, notes = self._attach({"target_root": target, "request": text, "attachments": files}, follow_up=True)

        def add(run):
            run.setdefault("messages", []).append({"text": text, "at": view.now(), "delivered": False})
            if run.get("status") == "done":
                run["status"] = "stopped"
                run.pop("handoff", None)
                run["revision"] = True  # 끝난 결과를 고치는 요청 — 실행기가 그에 맞는 지시를 붙인다
                if run.get("accepted"):  # 수락한 것은 그때의 결과 — 추가 요청으로 바뀐 결과는 다시 보고 수락한다
                    run.setdefault("accepted_before", []).append(run.get("accepted_at") or view.now())
                    run["accepted"] = False
                    run.pop("accepted_at", None)

        inbox.update_run(self.settings, run_id, add)
        inbox.log(self.settings, run_id, "user_message", text=text)
        return {"queued": True, "notes": notes}

    @Slot(str, str, result=str)
    @safe
    def pasteImage(self, b64, ext):
        """붙여 넣은 그림(클립보드 스크린샷)을 파일로 — 첨부와 같은 길로 넘길 수 있게 툴킷 홈에 둔다."""
        import base64
        import time

        ext = (ext or "png").lower().lstrip(".")
        if ext not in ("png", "jpg", "jpeg", "gif", "webp"):
            raise ToolError("invalid_request", "붙여 넣을 수 있는 그림은 png · jpg · gif · webp 입니다")
        data = base64.b64decode(b64.split(",", 1)[-1])
        if len(data) > 20 * 1024 * 1024:
            raise ToolError("invalid_request", "첨부 파일이 너무 큽니다 (20MB 까지): 붙여 넣은 그림")
        folder = self.settings.home / "pasted"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        f, n = folder / f"pasted-{stamp}.{ext}", 2
        while f.exists():
            f, n = folder / f"pasted-{stamp}-{n}.{ext}", n + 1
        f.write_bytes(data)
        return {"path": str(f)}

    @Slot(str, result=str)
    @safe
    def clearTamper(self, run_id):
        """사용자가 앱 밖에서 바뀐 결정을 확인했다 — 다시 돌릴 수 있게 표시를 지운다 (승인 기록은 그대로 남는다)."""
        r = inbox.update_run(self.settings, run_id, lambda r: r.pop("tampered", None))
        inbox.log(self.settings, run_id, "tamper_ack")
        return {"ok": True, "status": r.get("status")}

    @Slot(str, result=str)
    @safe
    def accept(self, run_id):
        integrity.record(run_id, "accept")
        inbox.update_run(self.settings, run_id, lambda r: r.update(accepted=True, accepted_at=view.now()))
        inbox.log(self.settings, run_id, "accepted")
        return {"accepted": True}

    @Slot(str, str, result=str)
    @safe
    def restore(self, run_id, checkpoint_id):
        if self._busy(run_id):
            raise ToolError("invalid_request", "진행 중에는 되돌릴 수 없습니다. 먼저 멈춰 주세요")
        run = inbox.load_run(self.settings, run_id)
        cp = Checkpoints(inbox.run_dir(self.settings, run_id), run["target_root"])
        label = next((c["label"] for c in cp.list() if c["id"] == checkpoint_id), "")
        out = cp.restore(checkpoint_id)
        inbox.log(self.settings, run_id, "restored", checkpoint=checkpoint_id, label=label)
        return out

    @Slot(str, str, result=str)
    @safe
    def revertFile(self, run_id, path):
        if self._busy(run_id):
            raise ToolError("invalid_request", "진행 중에는 되돌릴 수 없습니다")
        run = inbox.load_run(self.settings, run_id)
        cp = Checkpoints(inbox.run_dir(self.settings, run_id), run["target_root"])
        base = cp.list()[-1]["id"] if cp.list() else None
        if not base:
            raise ToolError("not_found", "체크포인트가 없습니다")
        from beom_workbench.core.checkpoints import _git
        root = Path(run["target_root"])
        if not inbox._inside(path, root) or (root / path).resolve() == root.resolve():
            raise ToolError("invalid_request", "대상 폴더 밖 경로")
        target = (root / path).resolve()
        exists = _git(cp.shadow, root, "cat-file", "-e", f"{base}:{path}", check=False).returncode == 0
        if exists:
            _git(cp.shadow, root, "checkout", "-q", base, "--", path)
        elif target.is_file():
            target.unlink()
        inbox.log(self.settings, run_id, "restored", label=f"{path} 되돌림")
        return {"reverted": path}

    @Slot(str, result=str)
    @safe
    def branchStatus(self, run_id):
        """작업 브랜치가 develop(시험)·main(적용)에 들어갔는지 — 합치기 메뉴용."""
        from beom_workbench.core import workbranch
        wb = (inbox.load_run(self.settings, run_id) or {}).get("work_branch") or {}
        return {"branch": wb.get("branch"), "repos": [workbranch.merge_status(r["repo"], r["branch"]) for r in wb.get("repos") or []
                                                      if Path(r["repo"]).exists()]}

    @Slot(str, str, result=str)
    @safe
    def mergeRun(self, run_id, into):
        """사용자가 누른 합치기 — into: 'develop'(시험) 또는 'main'(적용). 저장소마다 작업 브랜치를 그 저장소의 develop·main 에 합친다.
        체크아웃된 작업 폴더는 건드리지 않는다. 진행 중에는 하지 않는다 (라운드 중간 상태가 섞이지 않게)."""
        from beom_workbench.core import workbranch
        if into not in ("develop", "main"):
            raise ToolError("invalid_request", "develop 또는 main 에만 합칩니다")
        if self._busy(run_id):
            raise ToolError("invalid_request", "진행 중에는 합치지 않습니다 — 라운드가 끝난 뒤 눌러 주세요")
        wb = (inbox.load_run(self.settings, run_id) or {}).get("work_branch") or {}
        repos = [r for r in wb.get("repos") or [] if Path(r["repo"]).exists()]
        if not repos:
            raise ToolError("not_found", "이 작업에는 합칠 작업 브랜치가 없습니다")
        results, problems = [], []
        for r in repos:
            st = workbranch.merge_status(r["repo"], r["branch"])
            target = st["develop"] if into == "develop" else st["main"]
            if not target:
                problems.append(f"{st['name']}: {'develop' if into == 'develop' else 'main'} 브랜치가 없습니다")
                continue
            try:
                results.append(workbranch.merge(r["repo"], r["branch"], target))
            except ToolError as e:
                problems.append(e.message)
        label = "develop 에 합쳐 시험" if into == "develop" else "main 에 적용"
        done = [f"{x['repo']} → {x['into']} {x['commit']}" for x in results if x["merged"]]
        same = [f"{x['repo']}" for x in results if x["already"]]
        clash = [f"{x['repo']}: 충돌 — {', '.join(x['conflicts'][:6])}" for x in results if x["conflicts"]]
        parts = ([f"합침 {', '.join(done)}"] if done else []) + ([f"이미 들어 있음 {', '.join(same)}"] if same else []) + clash + problems
        ok = bool(done or same) and not clash and not problems
        inbox.log(self.settings, run_id, "merged", into=into, ok=ok, text=f"{label} · " + " · ".join(parts))
        return {"ok": ok, "results": results, "problems": problems + clash, "text": " · ".join(parts)}

    # -- 운영체제
    @Slot(result=str)
    @safe
    def pickFolder(self):
        return QFileDialog.getExistingDirectory(self.window, "폴더 선택") or ""

    @Slot(str, result=str)
    @safe
    def folderNeeds(self, path):
        """고른 폴더를 빌드하는 데 필요한데 이 PC 에 없는 도구 (Rust·OpenSSL·Node·JDK·Go)."""
        from beom_workbench.core import prereqs
        return prereqs.needs(path) if path else []

    @Slot(str, result=str)
    @safe
    def installTool(self, tool_id):
        """빌드 도구를 공식 패키지로 설치 — 정해 둔 명령을 보이는 터미널에서 연다 (사용자가 진행을 보고 확인한다)."""
        from beom_workbench.core import prereqs
        tool = prereqs.TOOLS.get(tool_id)
        if not tool:
            raise ToolError("not_found", "모르는 도구입니다")
        other = tool[4] if tool[4].startswith(("sudo ", "curl ")) else None  # Linux — 배포판 패키지 명령이 있으면 그대로
        cmd = tool[2] if os.name == "nt" else tool[3] if sys.platform == "darwin" else other
        if not cmd:
            raise ToolError("not_found", f"직접 설치해 주세요: {tool[4]}")
        _open_terminal(cmd, shell=True)
        return {"opened": True, "command": cmd}

    @Slot(str, result=str)
    @safe
    def folderGit(self, path):
        from beom_workbench.core import workbranch
        return workbranch.info(path) if path else None

    # -- 백그라운드 프로세스: 이 실행이 띄운 것(에이전트·개발 서버·남은 앱)을 보여 주고 끈다
    @Slot(str, result=str)
    @safe
    def runProcs(self, run_id):
        """지난번 결과를 바로 돌려주고 뒤에서 새로 본다 — 모든 프로세스를 훑는 일이라 화면(UI 스레드)을 멈추지 않게."""
        from beom_workbench.core import procs
        cache = self.__dict__.setdefault("_procs", {})
        busy = self.__dict__.setdefault("_procs_busy", set())
        if run_id not in busy:
            busy.add(run_id)

            def work():
                try:
                    cache[run_id] = procs.list_for(inbox.load_run(self.settings, run_id))
                except Exception:  # noqa: BLE001 — 목록을 못 봐도 화면은 그대로
                    pass
                finally:
                    busy.discard(run_id)

            threading.Thread(target=work, daemon=True).start()
        return cache.get(run_id, [])

    @Slot(str, int, result=str)
    @safe
    def killProc(self, run_id, pid):
        from beom_workbench.core import procs
        run = inbox.load_run(self.settings, run_id)
        if pid == run.get("agent_pid"):  # 에이전트는 실행 멈춤으로 (라운드 기록이 남게)
            if run_id in self.runners:
                self.runners[run_id].stop()
            if runner.lock_owner(self.settings, run_id) is not None:
                runner.request_stop(self.settings, run_id)
            return {"stopping": True}
        return procs.kill(run, pid)

    @Slot(str, result=str)
    @safe
    def openFolder(self, path):
        # 폴더만 연다 — 파일 경로를 주면 기본 프로그램으로 실행돼 버린다 (에이전트가 만든 .exe·.bat 등)
        if not path or not Path(path).is_dir():
            raise ToolError("invalid_request", "폴더만 열 수 있습니다")
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        return True

    @Slot(str, result=str)
    @safe
    def loginAgent(self, agent_id):
        st = agents.get(agent_id).status()
        cmd = st.get("login")
        if not cmd:
            raise ToolError("not_found", st.get("hint") or "로그인 명령이 없습니다")
        _open_terminal(cmd)
        self._agents_at = 0  # 터미널에서 로그인을 마치고 창으로 돌아오면 바로 다시 확인
        return {"opened": True}

    @Slot(result=str)
    @safe
    def checkUpdate(self):
        """새 버전이 나왔는지 뒤에서 한 번 확인한다 (updateFound 로 알림) — 공개 릴리스 목록만 읽는다."""
        from beom_workbench import __version__
        from beom_workbench.core import update
        if not update.enabled():
            return {"checking": False}

        def work():
            got = update.latest(__version__)
            if got:
                self.updateFound.emit(json.dumps(got))

        threading.Thread(target=work, daemon=True).start()
        return {"checking": True}

    @Slot(str, result=str)
    @safe
    def checkLink(self, url):
        """첨부한 링크를 뒤에서 한 번 읽어 본다 — 로그인이 필요하면 화면이 로그인을 요청한다 (linkChecked 로 알림)."""
        from beom_workbench.core import docs
        if not re.match(r"https?://", url or ""):
            raise ToolError("invalid_request", "http(s) 주소만 됩니다")

        def work():
            try:
                got = docs.fetch_url(url, timeout=12)
            except Exception as e:  # noqa: BLE001
                got = {"ok": False, "error": str(e)[:200]}
            self.linkChecked.emit(json.dumps({"url": url, "ok": bool(got.get("ok")), "needs_login": bool(got.get("needs_login")),
                                              "error": got.get("error"), "title": got.get("title")}, ensure_ascii=False))

        threading.Thread(target=work, daemon=True).start()
        return {"checking": True}

    @Slot(str, result=str)
    @safe
    def loginLink(self, url):
        """로그인이 필요한 링크를 앱 안의 브라우저 창으로 연다 — 사용자가 로그인하고 '이 페이지 가져오기'를 누르면 글을 첨부로."""
        from beom_workbench.app.linkbrowser import LinkBrowser
        if not re.match(r"https?://", url or ""):
            raise ToolError("invalid_request", "http(s) 주소만 됩니다")
        dlg = LinkBrowser(self.settings, url, self.window)
        dlg.saved.connect(lambda u, p: self.linkSaved.emit(json.dumps({"url": u, "path": p}, ensure_ascii=False)))
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.show()
        dlg.raise_()
        return {"opened": True}

    @Slot(result=str)
    @safe
    def engineStatus(self):
        """분석 엔진 상태 — 확인은 프로그램을 띄우는 일이라 뒤에서 하고, 화면에는 지난 값을 바로 준다."""
        cache = self.__dict__.setdefault("_engines", {"at": 0.0, "data": None})
        if time.monotonic() - cache["at"] > 120 and not cache.get("busy"):
            cache["busy"] = True

            def work():
                try:
                    cache["data"], cache["at"] = _engine_status(self.settings), time.monotonic()
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    cache["busy"] = False

            threading.Thread(target=work, daemon=True).start()
        return cache["data"]

    @Slot(result=str)
    @safe
    def installEngines(self):
        st = _engine_status(self.settings)
        if st.get("ready"):
            return {"opened": False, "note": "이미 준비돼 있습니다"}
        _open_terminal(st["install"], shell=True)
        self.__dict__.setdefault("_engines", {})["at"] = 0.0  # 돌아오면 다시 확인
        return {"opened": True, "command": st["install"]}

    @Slot(result=str)
    @safe
    def sandboxStatus(self):
        return _sandbox_status()

    @Slot(result=str)
    @safe
    def installSandbox(self):
        """격리 실행 구성 — 이 PC 에 샌드박스가 없으면 정해 둔 설치 명령을 보이는 터미널에서 연다."""
        st = _sandbox_status()
        if st.get("ready"):
            return {"opened": False, "note": "이미 준비돼 있습니다"}
        if not st.get("install"):
            raise ToolError("not_found", st.get("note") or "이 PC 에서는 격리 실행을 구성할 수 없습니다")
        _open_terminal(st["install"], shell=True)
        return {"opened": True, "command": st["install"]}

    @Slot(result=str)
    @safe
    def installAgents(self):
        """처음 설정 — 설치 안 된 에이전트와 격리 실행 도구를 터미널 하나에서 차례로 설치한다 (공식 단독 설치 — Node 필요 없음)."""
        cmds = []
        for a in agents.statuses(check=False):
            cmd = agents.install_command(a["id"])
            if not a.get("exe") and cmd:
                cmds.append((a["name"], cmd))
        sb = _sandbox_status()
        if not sb.get("ready") and sb.get("install") and all(c != sb["install"] for _, c in cmds):
            cmds.append(("격리 실행", sb["install"]))
        try:
            en = _engine_status(self.settings)
        except Exception:  # noqa: BLE001
            en = {"ready": True}
        if not en.get("ready") and en.get("install"):
            cmds.append(("분석 엔진", en["install"]))
        if not cmds:
            return {"opened": False, "note": "설치할 에이전트가 없습니다"}
        sep = "; " if os.name == "nt" else " ; "
        line = sep.join((f"Write-Host '== {n} 설치 =='; {c}" if os.name == "nt" else f"echo '== {n} 설치 =='; {c}") for n, c in cmds)
        _open_terminal(line, shell=True)
        self._agents_at = 0
        return {"opened": True, "agents": [n for n, _ in cmds]}

    @Slot(str, result=str)
    @safe
    def installAgent(self, agent_id):
        """설치 명령(코드에 정해 둔 것)을 보이는 터미널에서 연다 — 사용자가 진행 상황을 보고 끝나면 창으로 돌아온다."""
        st = agents.get(agent_id).status()
        cmd = st.get("install")
        if not cmd:
            raise ToolError("not_found", st.get("hint") or "설치 명령이 없습니다")
        _open_terminal(cmd, shell=True)
        self._agents_at = 0
        return {"opened": True, "command": cmd}
