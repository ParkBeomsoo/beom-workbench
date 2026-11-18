"""자동 실행용 도구 — 실행기가 띄운 Claude Code 에서만 쓰인다(BEOM_WORKBENCH_RUN). 권한 판단과 사람에게 묻기."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from beom_workbench.core import inbox
from beom_workbench.core.mask import mask_command
from beom_workbench.server import mcp, settings


def _policy_docs(run: dict) -> list[str]:
    g = run.get("guides") or {}
    return (g.get("hubs") or []) + (g.get("docs") or [])


def _run() -> dict | None:
    rid = os.environ.get("BEOM_WORKBENCH_RUN")
    return inbox.load_run(settings, rid) if rid else None


def _readable(inp: dict | None, root: Path) -> str:
    """권한 요청을 사람이 읽을 글로 — 명령·파일·주소·검색어 그대로 (JSON 으로 감싸면 경로의 \\ 가 두 겹으로 보인다).
    대상 폴더 경로는 '.' 으로 줄인다."""
    inp = inp or {}
    body = next((inp[k] for k in ("command", "file_path", "path", "url", "query", "pattern") if inp.get(k)), None)
    text = str(body) if body is not None else json.dumps(inp, ensure_ascii=False)
    for form in {str(root), str(root).replace("\\", "/")}:
        text = text.replace(form, ".")
    return " ".join(text.split())


# structured_output=False: Claude Code 는 권한 처리기의 결과로 '글자 블록 하나'만 받는다. 반환형이 str 이면 MCP SDK 2.x 가
# {"result": …} 구조화 결과를 함께 보내는데, 그러면 "Permission prompt tool returned an invalid result" 로 도구 호출이 실패한다.
@mcp.tool(structured_output=False)
def wb_action_approve(tool_name: str, input: dict, tool_use_id: str | None = None) -> str:  # noqa: A002 — Claude Code 가 정한 이름
    """Claude Code 의 권한 요청 처리기(--permission-prompt-tool). 종류별 규칙으로 허용·거절하고, 물어봐야 하면 앱의 승인 카드로 묻는다."""
    run = _run()
    if run is None:
        return json.dumps({"behavior": "deny", "message": "자동 실행 밖에서는 쓸 수 없음"}, ensure_ascii=False)
    root = Path(run["target_root"])
    if tool_name in ("Bash", "PowerShell") and inbox.foreground_server(input or {}):
        # 끝나지 않는 개발 서버를 앞에서 돌리면 라운드가 무응답 한도(기본 30분)까지 멈춘다 — 규칙(자동 포함)과 상관없이 돌려보낸다
        inbox.log(settings, run["id"], "permission", tool=tool_name, permission_kind="commands", decision="refuse",
                  what=mask_command(str((input or {}).get("command"))[:160]), why="foreground_server")
        return json.dumps({"behavior": "deny", "message": inbox.SERVER_HINT}, ensure_ascii=False)
    kind = inbox.classify(tool_name, input or {}, root, [settings.home])
    decision = inbox.decide(run, kind)
    if decision == "ask" and tool_name == "Bash" and inbox.planned_command(run, (input or {}).get("command", "")):
        decision = "allow"  # 사람이 승인한 계획에 그대로 있던 명령 — 다시 묻지 않는다
    shell_cmd = str((input or {}).get("command") or "") if tool_name in ("Bash", "PowerShell") else ""
    rule = inbox.command_rule(shell_cmd, root) if shell_cmd and kind == "commands" else None
    if decision == "ask" and rule and inbox.matches_rule(run, shell_cmd, root):
        decision = "allow"  # 사람이 '이 명령은 항상'으로 허락한 꼴
    from beom_workbench.core import sandbox
    wrap = tool_name == "PowerShell" and run.get("agent", "claude") == "claude" and sandbox.wraps_powershell(run) \
        and bool((input or {}).get("command"))
    # Node 도구는 Windows 격리 안에서 돌지 않는다 — 격리 없이 돌릴지는 별도 종류(unsandboxed)의 규칙으로 정한다
    node = wrap and inbox.needs_unsandboxed(str((input or {}).get("command")))
    # 기록에 남는 글은 비밀을 가린다 (카드에는 사람이 판단할 수 있게 원래 입력을 그대로 보여 준다)
    summary = mask_command(f"{inbox.KINDS.get(kind, kind)}: {tool_name} {_readable(input, root)[:300]}")
    what = mask_command(str((input or {}).get("command") or (input or {}).get("file_path") or (input or {}).get("url") or "")[:160])
    inbox.log(settings, run["id"], "permission", tool=tool_name, permission_kind=kind, decision=decision, what=what)
    outside_ok = node and inbox.decide(run, "unsandboxed") == "allow"
    if decision == "ask":
        note = " — 격리 없이 실행됩니다 (Node 빌드 도구는 격리 안에서 돌지 않음)" if node and not outside_ok else ""
        ans = inbox.ask(settings, run["id"], "permission", {"summary": summary + note, "tool": tool_name, "input": input,
                                                            "permission_kind": kind, **({"rule": rule} if rule else {})})
        if ans.get("always") and ans.get("allow"):
            inbox.update_run(settings, run["id"], lambda r: r["policy"].__setitem__(kind, "allow"))
        decision = "allow" if ans.get("allow") else "refuse"
        outside_ok = outside_ok or (node and decision == "allow")  # 사람이 격리 없이 실행된다는 것을 보고 허용했다
    elif decision == "allow" and node and not outside_ok and inbox.decide(run, "unsandboxed") == "ask":
        # 빌드처럼 바로 허용되는 명령이라도 격리 밖으로 나가는 것은 사람이 정한다 (한 번만 묻게 '이 프로젝트에서 항상'을 고를 수 있다)
        ans = inbox.ask(settings, run["id"], "permission", {
            "summary": mask_command(f"{inbox.KINDS['unsandboxed']}: {tool_name} {_readable(input, root)[:300]}"),
            "tool": tool_name, "input": input, "permission_kind": "unsandboxed"})
        if ans.get("always") and ans.get("allow"):
            inbox.update_run(settings, run["id"], lambda r: r["policy"].__setitem__("unsandboxed", "allow"))
        outside_ok = bool(ans.get("allow"))  # 거절하면 격리 안에서 그대로 돌린다 (Node 도구는 실패할 수 있다)
    if decision == "allow":
        if wrap and not outside_ok:
            # 격리 실행 — 허용한 명령을 Windows 샌드박스 안에서 (대상 폴더·임시·도구 캐시 밖에는 못 쓴다)
            input = {**input, "command": sandbox.wrap_powershell(input["command"], run)}  # noqa: A001
        elif wrap:
            inbox.log(settings, run["id"], "permission", tool=tool_name, permission_kind="unsandboxed", decision="allow", what=what)
        return json.dumps({"behavior": "allow", "updatedInput": input}, ensure_ascii=False)
    return json.dumps({"behavior": "deny", "message": f"규칙상 허용되지 않음 ({inbox.KINDS.get(kind, kind)}). 다른 방법을 찾거나 "
                                                      "wb_person_ask 로 사용자에게 이유와 함께 물어본다."}, ensure_ascii=False)


def _native_shot(run: dict, app, width, height) -> dict:
    """데스크톱 앱 창 찍기 — 검증 도구의 앱 화면 케이스와 같은 방식 (같은 권한 규칙)."""
    from beom_workbench.core import native

    if not native.available():
        return {"ok": False, "error": "데스크톱 앱 화면 찍기는 지금 Windows 에서만 됩니다"}
    root = Path(run["target_root"]).resolve()
    spec = {"app": app, "wait_ms": 1200, **({"width": int(width), "height": int(height)} if width and height else {})}
    inbox.gate_commands(settings, {"screen_app": native.command_text(spec)})
    out_dir = root / ".beom" / "shots"
    out_dir.mkdir(parents=True, exist_ok=True)
    name = "app-" + str(len(list(out_dir.glob("app-*.png"))) + 1)
    r = native.capture(spec, root, str(out_dir / f"{name}.png"))
    shot = r.get("out") if r.get("ok") else None
    if shot:
        inbox.log(settings, run["id"], "screen_shots", shots=[shot])
    return {"ok": bool(r.get("ok")), "shots": [{"app": native.command_text(spec), "ok": bool(r.get("ok")), "shot": shot,
                                                 "title": r.get("title"), "errors": (r.get("errors") or [])[:3]}]}


@mcp.tool()
def wb_person_ask(question: str, choices: list[str] | None = None) -> dict:
    """사용자에게 묻고 답을 기다린다(앱의 질문 카드). 요구사항이 애매하거나 결정이 필요할 때. 답이 없으면 timeout."""
    run = _run()
    if run is None:
        return {"ok": False, "error": "자동 실행 밖에서는 사용자에게 직접 묻는다"}
    ans = inbox.ask(settings, run["id"], "question", {"question": question, "choices": choices or []})
    return {"ok": not ans.get("timeout"), "answer": ans.get("text") or ans.get("choice"), "timeout": bool(ans.get("timeout"))}


def _update(fn) -> dict:
    run = _run()
    if run is None:
        return {"ok": False, "error": "자동 실행 밖"}
    inbox.update_run(settings, run["id"], fn)
    return {"ok": True}


@mcp.tool()
def wb_activity(text: str) -> dict:
    """지금 하는 일을 한 줄로 알린다 (앱 상단의 활동 표시). 예: "order.rs 에 할인 계산 추가 중"."""
    out = _update(lambda r: r.update(activity={"text": text.strip()[:200], "at": inbox._now()}))
    if out["ok"]:
        inbox.log(settings, os.environ["BEOM_WORKBENCH_RUN"], "activity", text=text.strip()[:200])
    return out


@mcp.tool()
def wb_plan_set(steps: list[dict], summary: str = "", design: str = "", decisions: list[dict] | None = None,
                 look: list[str] | None = None, previews: list[str] | None = None) -> dict:
    """구현 계획을 낸다 — 사용자가 앱에서 승인한 뒤 구현한다. steps: [{title, files?: [...], commands?: [...], requirements?: ["R1"]}].
    decisions: 사전 설계 결정 [{topic: "데이터 저장", choice: "SQLite 파일", options: ["JSON 파일"], why: "한 줄"}] — 형태(웹·앱·API)·
    데이터 저장·스택·실행 방법·디자인 방향처럼 결과를 크게 가르는 것. 사용자가 승인하며 대안으로 바꿀 수 있다.
    look: 디자인 컨셉 1~2줄, 전문 용어 없이 (사용자용). 색·글꼴 같은 수치는 계획에 쓰지 말고 구현의 토큰 파일에.
    design·previews 는 예전 형식 — 앱이 보여 주지 않는다 (승인 전에는 미리보기를 그리지 않는다).
    계획을 다시 내면 이전 승인은 풀린다."""
    clean = [{"title": str(s.get("title", "")).strip()[:200], "files": [str(f) for f in s.get("files") or []][:20],
              "commands": [str(c) for c in s.get("commands") or []][:10], "requirements": s.get("requirements") or [],
              "status": "todo"} for s in steps if str(s.get("title", "")).strip()]
    if not clean:
        return {"ok": False, "error": "단계가 비어 있음"}
    picks = []
    for d in decisions or []:
        if not isinstance(d, dict) or not str(d.get("topic", "")).strip() or not str(d.get("choice", "")).strip():
            continue
        choice = str(d["choice"]).strip()[:120]
        opts = [str(o).strip()[:120] for o in d.get("options") or [] if str(o).strip() and str(o).strip() != choice][:3]
        picks.append({"topic": str(d["topic"]).strip()[:60], "choice": choice, "options": opts, "why": str(d.get("why", "")).strip()[:300]})

    shots, bad = [], []
    run = _run()
    if previews and run is not None:
        base = Path(run["target_root"]) / ".beom"
        for x in previews[:4]:
            f = Path(x) if Path(x).is_absolute() else Path(run["target_root"]) / x
            if f.suffix.lower() == ".png" and f.is_file() and inbox._inside(str(f.resolve()), base.resolve()):
                shots.append(f.resolve().relative_to(Path(run["target_root"]).resolve()).as_posix())
            else:
                bad.append(str(x))
    lines = [str(x).strip()[:200] for x in look or [] if str(x).strip()][:6]

    def put(r):
        old = r.get("plan") or {}
        history = (old.get("history") or []) + ([{"steps": old["steps"], "summary": old.get("summary")}] if old.get("steps") else [])
        r["plan"] = {"steps": clean, "summary": summary.strip()[:1000], "approved": False, "history": history[-5:],
                     "at": inbox._now(), **({"design": design.strip()[:6000]} if design.strip() else {}),
                     **({"decisions": picks[:10]} if picks else {}), **({"look": lines} if lines else {}),
                     **({"previews": shots} if shots else {})}
    out = _update(put)
    if out["ok"]:
        inbox.log(settings, os.environ["BEOM_WORKBENCH_RUN"], "plan", steps=len(clean))
    if bad:
        out["warning"] = "미리보기로 쓸 수 없는 경로(대상 폴더 .beom/ 안 png 만): " + ", ".join(bad)
    return {**out, "note": "사용자 승인 전에는 코드를 바꾸지 않는다"}


@mcp.tool()
def wb_plan_progress(index: int, status: str, note: str = "") -> dict:
    """계획 단계 진행 표시. index 는 0부터. status: todo | doing | done | skipped."""
    if status not in ("todo", "doing", "done", "skipped"):
        return {"ok": False, "error": "status 는 todo|doing|done|skipped"}

    def put(r):
        steps = (r.get("plan") or {}).get("steps") or []
        if 0 <= index < len(steps):
            steps[index]["status"] = status
            if note:
                steps[index]["note"] = note.strip()[:300]
            if status == "doing":  # 진행 줄에도 — 따로 알리지 않는 에이전트(Codex 등)도 지금 단계가 보이게 (wb_activity 가 오면 그것으로 바뀐다)
                title = str(steps[index].get("title") or "").strip()[:150]
                r["activity"] = {"text": f"{index + 1}/{len(steps)}단계 진행 중 — {title}" if title else f"{index + 1}/{len(steps)}단계 진행 중",
                                 "at": inbox._now()}
    return _update(put)


def _items(v, limit: int = 8, size: int = 300) -> list[str] | str:
    """목록으로 받은 항목은 한 줄씩 다듬고, 글 한 덩어리는 그대로 둔다 (예전 형식 호환)."""
    if isinstance(v, (list, tuple)):
        return [str(x).strip()[:size] for x in v if str(x).strip()][:limit]
    return str(v or "").strip()[:2000]


@mcp.tool()
def wb_handoff(summary: str, how_to_run: str | list[str], risks: str | list[str] = "", follow_ups: str | list[str] = "",
                changes: list[str] | None = None, checked: list[str] | None = None) -> dict:
    """인수인계 — 완료 조건을 채운 뒤 남긴다. 사용자가 한눈에 보게 짧게, 항목은 목록으로:
    summary: 결과 한두 문장 (사용자 말로 — 무엇을 만들었고 지금 무엇을 할 수 있는지).
    changes: 사용자가 체감하는 변화 3~6개 (파일 이름이 아니라 기능으로).
    how_to_run: 실행 단계 목록 — 한 줄에 한 단계, 명령·경로는 `백틱`.
    checked: 어떻게 확인했는지 2~4개 (예: "합격 케이스 35개 모두 통과", "PC·모바일 화면을 찍어 직접 봄").
    risks: 남은 위험·확인 못 한 것, follow_ups: 이어서 할 일 — 각각 한 줄씩 최대 5개.
    내부 도구 이름·케이스 id·파일 목록은 쓰지 않는다 (앱이 따로 보여 준다).
    기획 모드에서는 기획 패키지 점검(wb_brief_check)의 problems 가 없어야 남겨진다."""
    run = _run()
    if run is not None and run.get("mode") == "plan":
        from beom_workbench.core import brief

        chk = brief.check(run["target_root"], _policy_docs(run))
        if not chk["ok"]:
            return {"ok": False, "error": "기획 패키지가 아직 맞물리지 않음 — 고친 뒤 다시 인계한다", "problems": chk["problems"]}
    data = {"summary": summary.strip()[:1500], "how_to_run": _items(how_to_run), "risks": _items(risks, 6),
            "follow_ups": _items(follow_ups, 6), "at": inbox._now()}
    if changes:
        data["changes"] = _items(changes, 8)
    if checked:
        data["checked"] = _items(checked, 6)
    out = _update(lambda r: r.update(handoff=data))
    if out["ok"]:
        inbox.log(settings, os.environ["BEOM_WORKBENCH_RUN"], "handoff")
    return out


@mcp.tool()
def wb_run_policy(changes: dict | None = None) -> dict:
    """이 실행의 권한 규칙 보기, 또는 더 엄격하게만 바꾸기 ({종류: ask|refuse}). 느슨하게 하는 변경은 무시된다."""
    run = _run()
    if run is None:
        return {"ok": False, "error": "자동 실행 밖"}
    if changes:
        run = inbox.update_run(settings, run["id"], lambda r: r.update(policy=inbox.tighten(r["policy"], changes)))
    return {"ok": True, "policy": run["policy"], "kinds": inbox.KINDS}


# ---------------------------------------------------------------- 기획 패키지 (기획 모드)

@mcp.tool()
def wb_brief_check() -> dict:
    """기획 패키지 점검 — .beom/brief/ 의 기획서·정책서·화면설계·테스트시나리오가 서로 맞물리는지 본다:
    빠진 문서·절, 테스트 시나리오가 없는 요구사항(R), 와이어프레임이 없는 화면(S), 스크린샷 안 찍은 와이어프레임, 열린 질문 수.
    problems 가 비어야 인계(wb_handoff)할 수 있다. notes 는 참고."""
    from beom_workbench.core import brief

    run = _run()
    if run is None:
        return {"ok": False, "error": "자동 실행 밖"}
    return brief.check(run["target_root"], _policy_docs(run))


@mcp.tool()
def wb_brief_screens(files: list[str] | None = None, mobile: bool = False) -> dict:
    """와이어프레임(.beom/brief/wireframes/*.html)을 브라우저 엔진으로 열어 스크린샷을 .beom/brief/shots/ 에 남긴다.
    files 를 비우면 전부. mobile=true 면 390×844 로 (파일 이름 끝에 -mobile). 깨짐·스크립트 오류가 있으면 errors 에 나온다 —
    스크린샷을 직접 열어 보고 겹침·잘림·빈 화면이 있으면 고친 뒤 다시 찍는다."""
    from beom_workbench.core import brief, screens

    run = _run()
    if run is None:
        return {"ok": False, "error": "자동 실행 밖"}
    if not screens.available():
        return {"ok": False, "error": "이 설치에는 화면 엔진(PySide6)이 없어 스크린샷을 찍을 수 없음 — 와이어프레임 HTML 만 남긴다"}
    out = brief.screens(run["target_root"], files, mobile)
    inbox.log(settings, run["id"], "brief_shots", shots=[s["shot"] for s in out["shots"] if s.get("shot")])
    return {"ok": True, **out}


@mcp.tool()
def wb_screen_shot(files: list[str] | None = None, mobile: bool = False, width: int | None = None, height: int | None = None,
                   app: list[str] | str | None = None, url: str | None = None, paths: list[str] | None = None,
                   cmd: str | None = None, actions: list[dict] | None = None) -> dict:
    """화면을 찍어 .beom/shots/ 에 남긴다 — 디자인을 스스로 검토할 때. 결과의 shot 경로를 직접 열어 보고 정렬·간격·위계·잘림을 고친다.
    HTML: files=["index.html"] — 브라우저 엔진으로 연다 (권한을 묻지 않는다, 대상 폴더만 127.0.0.1 로). mobile=true 면 390×844.
    서버가 있어야 보이는 화면: url="http://127.0.0.1:8000", paths=["/", "/t/abc"], cmd="python server.py" — 툴킷이 서버를 잠깐 띄워
    찍고 끈다 (cmd 는 내 코드를 돌리는 명령이라 권한 규칙을 거친다, 이미 떠 있으면 cmd 없이). 이 PC(localhost) 주소만.
    actions=[{"fill": "#name", "value": "점심"}, {"click": "#add"}, {"press": "Enter"}, {"wait_ms": 300}] — 누르고 입력한 뒤의 화면을
    찍는다 (files·url 모두). 눌러 본 뒤의 모습을 확인할 때 쓴다 — '클릭은 확인 못 함'으로 남기기 전에.
    데스크톱 앱(Windows, tkinter·Qt·WinForms·WPF 등): app=["python", "main.py"] 또는 "dist/App.exe" — 앱을 띄워 창을 찍고 닫는다.
    앱 실행은 내 코드를 돌리는 명령이라 권한 규칙을 거친다. tkinter 는 창 안 글자를 읽어 주지 않으니 모습은 그림으로 확인한다."""
    from beom_workbench.core import screens

    run = _run()
    if run is None:
        return {"ok": False, "error": "자동 실행 밖"}
    if app:
        return _native_shot(run, app, width, height)
    if url:
        return _url_shot(run, url, paths or ["/"], cmd, mobile, width, height, actions)
    if not files:
        return {"ok": False, "error": "files(HTML), url(서버 주소) 또는 app(데스크톱 앱 실행 명령)이 필요함"}
    if not screens.available():
        return {"ok": False, "error": "이 설치에는 화면 엔진(PySide6)이 없음"}
    root = Path(run["target_root"]).resolve()
    out_dir = root / ".beom" / "shots"
    out_dir.mkdir(parents=True, exist_ok=True)
    picks = []
    for f in files[:12]:
        p = (root / f).resolve()
        if not p.is_relative_to(root.resolve()) or not p.is_file() or p.suffix.lower() not in (".html", ".htm"):
            return {"ok": False, "error": f"대상 폴더 안의 HTML 파일이 아님: {f}"}
        picks.append(p)
    with screens.serve_folder(root) as srv:
        pages = []
        for p in picks:
            spec = {"file": p.relative_to(root).as_posix(), "mobile": mobile, "wait_ms": 700, "actions": list(actions or [])}
            if width and height:
                spec.update(width=int(width), height=int(height))
            name = p.relative_to(root).as_posix().replace("/", "_").rsplit(".", 1)[0] + ("-mobile" if mobile else "")
            pages.append(screens.page_spec(spec, None, root, str(out_dir / f"{name}.png"), file_base=srv.base))
        got = screens.capture(pages)
    shots = [{"file": p.relative_to(root).as_posix(), "ok": bool(r.get("ok")), "shot": r.get("out") if r.get("ok") else None,
              "errors": (r.get("errors") or [])[:3]} for p, r in zip(picks, got)]
    inbox.log(settings, run["id"], "screen_shots", shots=[s["shot"] for s in shots if s["shot"]])
    return {"ok": True, "shots": shots}


def _url_shot(run: dict, url: str, paths: list[str], cmd: str | None, mobile: bool, width, height, actions) -> dict:
    """서버 화면 — cmd 가 있으면 검증과 같은 방식으로 잠깐 띄워 찍고 끈다. 그동안 프로그램이 만든 파일(데이터 등)은 치운다."""
    from beom_workbench.core import port, screens
    if not screens.available():
        return {"ok": False, "error": "이 설치에는 화면 엔진(PySide6)이 없음"}
    port.check_url(url)
    if cmd:
        inbox.gate_commands(settings, {"screen_server": cmd})
    root = Path(run["target_root"]).resolve()
    out_dir = root / ".beom" / "shots"
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = []
    for pth in paths[:12]:
        pth = "/" + str(pth).lstrip("/")
        name = "url" + (re.sub(r"[^\w-]+", "_", pth).strip("_")[:60] or "") + ("-mobile" if mobile else "")
        spec = {"path": pth, "mobile": mobile, "wait_ms": 700, "actions": list(actions or [])}
        if width and height:
            spec.update(width=int(width), height=int(height))
        pages.append((pth, screens.page_spec(spec, url, root, str(out_dir / f"{name or 'url'}.png"))))
    before = port._files(root)
    server = None
    try:
        if cmd:
            server = port._Server(port._Side("target", root, root / ".beom"), cmd, url)
        got = screens.capture([pg for _, pg in pages])
    finally:
        if server:
            server.close()
        port._drop_new(root, before, keep=set())
    shots = [{"path": pth, "ok": bool(r.get("ok")), "shot": r.get("out") if r.get("ok") else None, "errors": (r.get("errors") or [])[:3]}
             for (pth, _), r in zip(pages, got)]
    inbox.log(settings, run["id"], "screen_shots", shots=[x["shot"] for x in shots if x["shot"]])
    return {"ok": True, "shots": shots}
