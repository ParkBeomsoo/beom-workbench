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
    if tool_name in ("Bash", "PowerShell") and _SHELL_WRITE.search(str((input or {}).get("command") or "")):
        from beom_workbench import hookgate
        why = hookgate.verdict(run, "Write", {"file_path": str(root / "_shell_write")})
        if why:  # 계획 전·승인 전에는 셸로도 제품 파일을 쓰지 않는다 (.beom 안 쓰기는 편집 도구로)
            inbox.log(settings, run["id"], "permission", tool=tool_name, permission_kind="edit", decision="refuse", why="order")
            return json.dumps({"behavior": "deny", "message": why + " 셸로 파일을 쓰지 않는다 — .beom/ 의 파일은 Write 도구로."},
                              ensure_ascii=False)
    # 작업 폴더가 저장소의 하위 폴더면 그 저장소 안의 다른 폴더는 읽기만 묻지 않는다 (고치기는 묻는다)
    kind = inbox.classify(tool_name, input or {}, root, [settings.home], read_roots=inbox.read_roots(run, settings))
    users = inbox.shared_target(run, tool_name, input or {}) if kind == "edit" else 0
    if users:
        kind = "shared"  # 다른 화면들이 쓰는 기존 공통 부품 — 계획에서 승인하지 않았으면 편집 모드에서도 사용자에게 묻는다
    decision = inbox.decide(run, kind)
    native = inbox.native_permissions(run)
    mode = inbox.mode_of(run.get("policy") or {})
    # 툴킷의 ask 규칙 때문에 여기로 온 공통 부품 편집인데 이미 풀렸다(신고 승인·'항상' 허락) — 사용자가 정한 것이니 다시 묻지 않는다
    freed = kind == "shared" and decision == "allow" or (kind == "edit" and _shared_rel(run, tool_name, input or {}) in
                                                        (run.get("shared_parts") or {}))
    if native and mode in ("edits", "ask") and decision == "allow" and tool_name not in inbox.READ_TOOLS and not freed:
        # 에이전트가 자기 모드에서 물어본 것 — 툴킷이 분류해 대신 허용하지 않고 사용자에게 묻는다 (아래에서 사용자가 이미
        # '항상'으로 허락한 규칙·승인한 계획의 명령이면 그대로 허용). 읽기는 원본·툴킷이 찍은 화면 같은 정해진 자리만 허용된다
        decision = "ask"
    redirect = None if native else _redirect(run, tool_name, input or {}, kind, decision)
    if redirect:
        inbox.log(settings, run["id"], "permission", tool=tool_name, permission_kind=kind, decision="redirect",
                  what=mask_command(str((input or {}).get("command"))[:160]))
        return json.dumps({"behavior": "deny", "message": redirect}, ensure_ascii=False)
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
    summary = mask_command(f"{inbox.action_label(kind, tool_name, input or {}, root, inbox.read_roots(run, settings))}"
                           f"{f' — 이 파일을 쓰는 다른 화면 {users}곳도 함께 바뀝니다' if users else ''}: "
                           f"{tool_name} {_readable(input, root)[:300]}")
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
    if kind == "shared":
        return json.dumps({"behavior": "deny", "message": "사용자가 이 공통 부품의 수정을 허락하지 않았다 — 다른 화면들이 쓰는 부품이다. "
                                                          "고치지 말고 새 화면 쪽에서 해결한다. 꼭 고쳐야 하면 계획(wb_plan_set)의 "
                                                          "shared_changes 로 이유와 함께 신고해 승인을 받는다."}, ensure_ascii=False)
    return json.dumps({"behavior": "deny", "message": f"규칙상 허용되지 않음 ({inbox.KINDS.get(kind, kind)}). 다른 방법을 찾거나 "
                                                      "wb_person_ask 로 사용자에게 이유와 함께 물어본다."}, ensure_ascii=False)


REDIRECT_TASK = ("이 명령은 사용자에게 물어야 하는 꼴이라 돌리지 않았습니다 (사용자에게 묻지도 않았습니다). 테스트·타입 검사·빌드·린트는 "
                 "mcp__beom-workbench__wb_task(task=\"test\"|\"typecheck\"|\"build\"|\"lint\") 로 돌립니다 — 명령·격리·권한·의존성은 "
                 "툴킷이 맡습니다. 일부만 돌리려면 paths(테스트 파일)나 name(테스트 이름)을 넘깁니다.")
REDIRECT_REMOVE = ("이 삭제 명령은 사용자에게 물어야 하는 꼴이라 돌리지 않았습니다 (사용자에게 묻지도 않았습니다). 파일·폴더 지우기는 "
                   "mcp__beom-workbench__wb_remove(paths=[...]) 로 합니다 — 이번 실행에서 만든 것과 .beom/tmp·.beom/helpers 는 묻지 않고 "
                   "지우고, 원래 있던 사용자 파일만 규칙대로 묻습니다 (대상 폴더 밖은 지우지 않습니다).")


REDIRECT_PROBE = ("짧은 코드로 동작을 알아보는 명령(python -c)은 사용자에게 물어야 하는 꼴이라 돌리지 않았습니다 (사용자에게 묻지도 "
                  "않았습니다). 알아볼 것을 .beom/tmp/test_probe.py 같은 테스트로 적고 mcp__beom-workbench__wb_task(task=\"test\", "
                  "paths=[\".beom/tmp/test_probe.py\"]) 로 돌립니다 — 결과를 print 하거나 assert 로 확인합니다 (-s 없이도 실패 메시지에 "
                  "값이 보이게 assert 에 넣는 것이 낫습니다).")
REDIRECT_WRITE = ("셸 스크립트로 파일 내용을 바꾸는 명령은 사용자에게 물어야 하는 꼴이라 돌리지 않았습니다 (사용자에게 묻지도 "
                  "않았습니다). 대상 폴더 안의 파일은 Edit·Write 도구로 고칩니다 — 여러 곳을 한꺼번에 바꿔야 하면 Write 로 파일을 통째로 "
                  "다시 씁니다 (계획·승인 뒤의 제품 파일과 .beom/ 은 묻지 않고 바로 된다 — 공통 부품은 신고·승인 뒤).")
REDIRECT_INSTALL = ("의존성 설치는 mcp__beom-workbench__wb_deps 로 합니다 — 툴킷이 프로젝트 자리(.beom/venv·node_modules)에 설치하고 "
                    "잠금 파일(package-lock.json)을 남긴다. 새 패키지는 requirements.txt·pyproject.toml·package.json 에 적은 뒤 wb_deps.")
_INSTALL_RX = re.compile(r"(?:^|[\s;&|(])(?:(?:npm|pnpm|yarn|bun)(?:\.cmd)?\s+(?:install|i|add|ci)\b(?![^;&|]*\s(?:-g|--global)\b)|"
                         r"(?:pip3?|uv\s+pip)\s+install\b|(?:python3?|py)(?:\.exe)?\s+-m\s+pip\s+install\b|poetry\s+(?:add|install)\b)", re.I)


class _InstallCmd:
    @staticmethod
    def search(cmd: str):
        return _INSTALL_RX.search(_QUOTED.sub("''", str(cmd)))


INSTALL_CMD = _InstallCmd()
_SHELL_WRITE_RX = re.compile(r"\b(?:Set-Content|Add-Content|Out-File|New-Item\b[^;|]*-Value)\b|\[(?:System\.)?IO\.File\]::(?:Write|Append)"
                             r"|(?<![<>\d=\-])>{1,2}(?![=>])\s*['\"]?[\w.\\/:-]+\.\w+", re.I)
_QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\`]|\\.|`.)*\"")


class _ShellWrite:
    """셸로 파일을 쓰는 명령인가 — 따옴표 안의 글(rg "=> res.data" 같은 검색어)은 빼고 본다."""

    @staticmethod
    def search(cmd: str):
        return _SHELL_WRITE_RX.search(_QUOTED.sub("''", str(cmd)))


_SHELL_WRITE = _ShellWrite()
# 파이썬 실행기 옵션(-X utf8 · -W ignore · -B -u 등) 뒤의 -c
_PY_PROBE = re.compile(r"(?:^|[\s;&|(\\/])(?:python3?|py)(?:\.exe)?\"?\s+(?:-[XW]\s*\S+\s+|-[BuIEsSOq]+\s+)*-c\s", re.I)


def _redirect(run: dict, tool_name: str, inp: dict, kind: str, decision: str) -> str | None:
    """물어야 하는 셸 명령 중 툴킷 도구가 맡는 일(테스트·빌드·삭제)이면 카드 대신 그 도구로 돌려보낸다 — 같은 일을 표기만 바꿔
    다시 묻는 일이 없게. 규칙상 바로 허용되는 명령은 그대로 돌고, 도구가 그 일을 못 하는 폴더면 원래대로 묻는다."""
    from beom_workbench.core import sandbox, tasks
    if tool_name not in ("Bash", "PowerShell"):
        return None
    cmd = str(inp.get("command") or "")
    asks = decision == "ask" or (decision == "allow" and tool_name == "PowerShell" and sandbox.wraps_powershell(run)
                                 and inbox.needs_unsandboxed(cmd) and inbox.decide(run, "unsandboxed") == "ask")
    if not asks:
        return None
    if inbox.DELETE_CMD.search(cmd):  # 먼저 — 'Remove-Item vitest.config.ts' 는 테스트가 아니라 삭제다
        return REDIRECT_REMOVE
    if _SHELL_WRITE.search(cmd):
        return REDIRECT_WRITE
    if INSTALL_CMD.search(cmd):
        return REDIRECT_INSTALL
    task = inbox.task_like(cmd)
    if task and tasks.available(Path(run["target_root"]), task):
        return REDIRECT_TASK
    if _PY_PROBE.search(cmd) and "python" in tasks.kinds(Path(run["target_root"])):
        # 파이썬 프로젝트면 — 테스트가 아직 없어도 확인용 테스트를 만들어 wb_task 로 돌리면 된다
        return REDIRECT_PROBE
    return None


@mcp.tool()
def wb_task(task: str, paths: list[str] | None = None, name: str = "", path: str = "", timeout: int = 600) -> dict:
    """프로젝트 작업을 툴킷이 돌린다 — task: "test"(테스트) · "typecheck"(타입 검사: tsc --noEmit·cargo check 등) · "build" · "lint".
    명령은 툴킷이 프로젝트 파일(package.json 스크립트·pyproject·Cargo.toml·go.mod 등)로 고르고, 의존성 준비·격리·PATH·권한·
    찌꺼기 치우기도 툴킷이 한다. 셸에서 npm test·pytest·tsc·cargo test 를 짜지 말고 이것을 부른다.
    paths: 일부 테스트 파일만 (대상 폴더 기준 상대 경로) · name: 테스트 이름으로 거르기 · path: 하위 폴더 프로젝트(모노레포).
    결과: 종류별 명령·종료 코드·요약 줄(통과·실패 수)·끝 출력."""
    from beom_workbench.core import tasks
    run = _run()
    if run is None:
        return {"ok": False, "error": "자동 실행 밖"}
    return tasks.run_task(settings, run, task, path=path, paths=paths, name=name, timeout=timeout)


@mcp.tool()
def wb_remove(paths: list[str]) -> dict:
    """대상 폴더 안의 파일·폴더를 지운다 (Remove-Item·rm 대신). 이번 실행에서 만든 것과 .beom/tmp·.beom/helpers 는 바로 지우고,
    실행 전부터 있던 사용자 파일은 셸로 지울 때와 같은 규칙대로 (편집 자동 허용이면 카드). 지운 것은 체크포인트로 되돌릴 수 있다.
    .beom 의 다른 파일(합격 케이스·venv)·.git 은 지우지 않는다."""
    from beom_workbench.core import tasks
    run = _run()
    if run is None:
        return {"ok": False, "error": "자동 실행 밖"}
    return tasks.remove(settings, run, paths)


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
def wb_deps() -> dict:
    """의존성 준비 — package.json·requirements.txt·pyproject.toml 을 쓰거나 고친 뒤 부른다. 툴킷이 같은 자리에 설치한다
    (Node: 프로젝트 node_modules · Python: .beom/venv 에 의존성과 pytest·coverage). 내용이 바뀔 때만 다시 설치하고, 권한 규칙대로
    한 번 묻는다. npm install·pip install·가상 환경 만들기를 직접 하지 않는다. 테스트는 wb_task 로 (명령에는 경로 없이 python).
    검증(wb_port_verify)·화면 찍기는 돌기 전에 스스로 이것을 부른다."""
    from beom_workbench.core import deps
    run = _run()
    if run is None:
        return {"ok": False, "error": "자동 실행 밖"}
    out = deps.ensure(settings, run, Path(run["target_root"]))
    failed = [x for x in out["installed"] if not x["ok"]]
    return {"ok": not failed, **out}


@mcp.tool()
def wb_run(command: str, timeout: int = 600) -> dict:
    """격리 밖에서 명령 하나를 돌린다 — 자체 샌드박스에서 인터넷이 막힌 에이전트(Codex)가 격리 안에서 안 되는 명령을 돌릴 때.
    의존성 설치(npm install·pip install 등)는 wb_deps, 테스트·빌드는 wb_task 로 한다 (여기로 보내면 그쪽으로 돌려보낸다). 권한 규칙을 그대로 거친다: 명령의 종류(빌드·명령 실행 등)와 '격리 없이 실행'이 모두 허용이면
    바로 돌고, 아니면 사용자에게 카드로 묻는다 (시스템 변경·게시·브랜치 바꾸기는 거절). 대상 폴더에서 PowerShell(Windows)·sh 로
    돌리고 종료 코드와 끝 출력을 돌려준다. 시간이 넘으면 끝낸다 (기본 600초, 최대 1800초).
    결과물(README·스크립트)에는 이 도구 이야기를 쓰지 않는다 — 사용자는 평소처럼 npm install 을 친다."""
    import subprocess
    import sys
    from beom_workbench.core import port, runtime

    run = _run()
    if run is None:
        return {"ok": False, "error": "자동 실행 밖"}
    if run.get("agent", "claude") != "codex":
        return {"ok": False, "error": "셸 도구로 실행한다 — 이 도구는 자체 샌드박스에서 인터넷이 막힌 에이전트(Codex)용"}
    cmd = (command or "").strip()
    if not cmd:
        return {"ok": False, "error": "command 가 비어 있음"}
    root = Path(run["target_root"])
    kind = inbox.classify("PowerShell", {"command": cmd}, root, [settings.home], read_roots=inbox.read_roots(run, settings))
    dk, du = inbox.decide(run, kind), inbox.decide(run, "unsandboxed")
    if "refuse" in (dk, du):
        inbox.log(settings, run["id"], "permission", tool="mcp:wb_run", permission_kind=kind if dk == "refuse" else "unsandboxed",
                  decision="refuse", what=mask_command(cmd[:160]))
        return {"ok": False, "error": f"규칙상 허용되지 않음 ({inbox.KINDS.get(kind if dk == 'refuse' else 'unsandboxed')}): {cmd[:200]}",
                "recovery": "다른 방법을 쓰거나 wb_person_ask 로 사용자에게 이유와 함께 묻는다"}
    if INSTALL_CMD.search(cmd):  # 설치는 툴킷이 프로젝트의 .beom/venv·node_modules 에 한다 — 셸로 깔면 엉뚱한 파이썬에 들어간다
        inbox.log(settings, run["id"], "permission", tool="mcp:wb_run", permission_kind=kind, decision="redirect", what=mask_command(cmd[:160]))
        return {"ok": False, "error": REDIRECT_INSTALL}
    known = inbox.approved_command(run, cmd) or inbox.matches_rule(run, cmd, root)
    if not known and (dk != "allow" or du != "allow"):
        from beom_workbench.core import tasks
        task = inbox.task_like(cmd)
        if task and tasks.available(root, task):
            inbox.log(settings, run["id"], "permission", tool="mcp:wb_run", permission_kind=kind, decision="redirect",
                      what=mask_command(cmd[:160]))
            return {"ok": False, "error": REDIRECT_TASK}
        rule = inbox.command_rule(cmd, root) if kind == "commands" else None
        extra = "" if kind in ("build", "edit") else f" — {inbox.KINDS.get(kind, kind)}"
        ans = inbox.ask(settings, run["id"], "permission", {
            "summary": mask_command(f"격리 밖 실행 (Codex 샌드박스는 인터넷이 막혀 있음){extra}: {cmd[:300]}"),
            "tool": "mcp:wb_run", "input": {"command": cmd}, "permission_kind": "unsandboxed", **({"rule": rule} if rule else {})})
        if not ans.get("allow"):
            return {"ok": False, "error": "사용자가 실행을 거절함: " + cmd[:200],
                    "recovery": "다른 방법을 쓰거나 wb_person_ask 로 사용자에게 이유와 함께 묻는다"}
        if ans.get("always"):  # '이 프로젝트에서 항상' — 격리 밖 실행을 허용으로 (명령 종류의 규칙은 그대로)
            inbox.update_run(settings, run["id"], lambda r: r["policy"].__setitem__("unsandboxed", "allow"))
        inbox.update_run(settings, run["id"], lambda r: r.update(approved_cmds=list(dict.fromkeys((r.get("approved_cmds") or []) + [cmd]))))
    inbox.log(settings, run["id"], "permission", tool="mcp:wb_run", permission_kind="unsandboxed", decision="allow", what=mask_command(cmd[:160]))
    if sys.platform == "win32":
        # 실행 정책은 건드리지 않는다 — PowerShell 에서 npm 이 npm.ps1(스크립트)로 연결돼 기본 정책(Restricted)에 막히는 것이 원인이라,
        # npm·npx·pnpm·yarn 을 같은 이름의 실행 파일(.cmd)로 부르게 한다 (함수가 외부 명령보다 먼저 — 그룹 정책 PC 에서도 같다)
        from beom_workbench.core import sandbox
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $OutputEncoding=[Text.Encoding]::UTF8; " + sandbox.ps_script(cmd)]
    else:
        argv = ["sh", "-c", cmd]
    env = runtime.safe_env({"PYTHONIOENCODING": "utf-8", **runtime.coverage_env(root)})
    try:
        cp = port.run_tree(argv, cwd=str(root), timeout=max(10, min(int(timeout or 600), 1800)), env=env)
    except subprocess.TimeoutExpired as e:
        tail = ((e.output or b"") + (e.stderr or b"")).decode("utf-8", "replace")[-3000:]
        return {"ok": False, "error": f"{timeout}초 안에 끝나지 않아 멈췄다", "output_tail": tail}
    out = (cp.stdout + cp.stderr).decode("utf-8", "replace")
    inbox.log(settings, run["id"], "unsandboxed_run", command=mask_command(cmd[:300]), exit_code=cp.returncode)
    return {"ok": cp.returncode == 0, "exit_code": cp.returncode, "output_tail": out[-4000:]}


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


def _started(run: dict) -> float | None:
    """작업을 시작한 시각 (초 단위로 잘려 기록되니 1초 여유)."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(run["created_at"]).timestamp() + 1
    except (KeyError, ValueError, TypeError):
        return None


def _screen_files(steps: list[dict]) -> list[str]:
    """계획이 만들·고칠 화면 파일 — HTML·JSX·Vue·템플릿·스타일. .js/.ts 만으로는 화면 작업이 아니다 (서버·CLI 도 .js/.ts)."""
    from beom_workbench.core import conventions
    exts = conventions.SCREEN_EXTS + (".css", ".scss", ".sass", ".less")
    return [x for s in steps for x in s["files"] if str(x).lower().endswith(exts)]


def _path_part(ref: str) -> str:
    """'경로:줄' · '경로:줄-줄' · '경로#L줄' 에서 경로만 — D:\\repo\\a.md 같은 드라이브 문자는 그대로 둔다."""
    import re as _re
    return _re.sub(r"(?::\d+(?:[-:]\d+)?|#L\d+(?:-L?\d+)?)(?:\s.*)?$", "", str(ref).strip())


def _follows(run: dict | None, steps: list[dict], follows: list[dict] | None) -> tuple[list[dict] | None, list[str]]:
    """계획의 '기존 규칙 따름' — 근거(파일:줄)가 실제 기존 코드인지 확인한다. 기존 화면이 여럿 있는 프로젝트에서 화면 파일을
    만들·고치는 계획인데 근거가 없으면 (None, [이유…]) — 계획을 받지 않는다. 틀린 근거는 빼고 알린다."""
    from beom_workbench.core import conventions
    if run is None:
        return [], []
    root = Path(run["target_root"])
    rules, errors = [], []
    for f in follows or []:
        if not isinstance(f, dict) or not str(f.get("rule", "")).strip():
            continue
        ok, bad = conventions.check_evidence(root, [str(x) for x in f.get("evidence") or []][:6])
        errors += [f"근거 확인 실패: {b}" for b in bad]
        if ok:
            rules.append({"rule": str(f["rule"]).strip()[:200], "evidence": ok})
    ui = _screen_files(steps)
    if ui and conventions.has_screens(root, before=_started(run)):
        files = {_path_part(e) for r in rules for e in r["evidence"]}
        if len(files) < 2:
            return None, ["기존 화면이 있는 프로젝트에서 화면(" + ", ".join(ui[:3]) + ")을 만들거나 고치는 계획인데, 다른 화면들의 규칙을 "
                          "확인한 근거가 없음 — wb_ui_conventions(near=[만들·고칠 파일])로 비슷한 기존 화면과 공통 부품의 쓰임을 보고, "
                          "follows=[{rule, evidence:[\"파일:줄\", …]}] 로 따를 규칙을 남긴다 (서로 다른 기존 파일 2개 이상)", *errors]
    return rules[:12], errors


def _rules_checked(run: dict | None, checked: list[str] | None, rules: list[dict] | None) -> tuple[list[str], str | None]:
    """대상 저장소의 규칙 문서(작업을 시작할 때 찾아 지시에 넣은 것)를 읽었다고 밝혔는지 — 에이전트 지시(AGENTS.md 등)·규칙 허브는
    모두, 정책·가이드 문서는 하나 이상. follows 근거로 든 문서도 읽은 것으로 친다. 규칙 문서가 없으면 묻지 않는다."""
    if run is None:
        return [], None
    g = run.get("guides") or {}
    root = Path(run["target_root"]).resolve()

    def norm(x: str) -> Path:
        return (root / _path_part(x)).resolve()

    # 에이전트 지시는 대상 폴더와 그 위의 것만 이번 작업에 적용된다 (옆 패키지·하위 폴더의 AGENTS.md 는 그 폴더를 고칠 때)
    on_path = {root, *root.parents}
    must = [a for a in g.get("agents", []) if norm(a).parent in on_path] + list(g.get("hubs", []))
    some = g.get("docs", [])
    said = [str(x) for x in checked or []][:30]
    if not must and not some:  # 찾아 둔 규칙 문서가 없다 — 묻지 않고, 에이전트가 읽었다고 밝힌 것은 남긴다
        return [x for x in said if norm(x).exists()], None

    got = {norm(x) for x in said} | {norm(e) for r in rules or [] for e in r["evidence"]}
    missing = [m for m in must if norm(m) not in got]
    doc_ok = not some or any(norm(d) in got or any(p.is_relative_to(norm(d)) for p in got) for d in some)
    if not missing and doc_ok:
        return [x for x in said if norm(x).exists()], None
    parts = []
    if missing:
        parts.append("에이전트 지시·규칙 허브 " + ", ".join(missing))
    if not doc_ok:
        parts.append("정책·가이드 문서(" + ", ".join(some[:5]) + ") 중 이번 작업에 닿는 것")
    return [], ("대상 저장소의 규칙 문서를 읽었다고 밝히지 않음: " + " · ".join(parts) +
                " — 읽고 rules_checked=[\"문서 경로\", …] 로 남기고, 따를 조항은 follows 근거(문서:줄)로 든다")


# 화면 디자인 방향 결정 — 'API design'·'코드 스타일'·'DB 스키마 디자인' 은 아니다
DESIGN_TOPIC = re.compile(r"디자인|(?:visual|ui|ux|screen|web)\s*design|design\s*direction|look\s*(?:&|and)\s*feel|룩앤필|시안", re.I)
DESIGN_NOT = re.compile(r"\b(?:api|db|database|schema|data|code|system|architecture)\b|스키마|데이터|코드|아키텍처|구조", re.I)


def _design_pick(picks: list[dict]) -> dict | None:
    """디자인 방향 결정 — 시안이 붙은 결정이 먼저, 없으면 주제가 화면 디자인인 결정."""
    return (next((d for d in picks if d.get("looks") or d.get("shots")), None)
            or next((d for d in picks if DESIGN_TOPIC.search(d["topic"]) and not DESIGN_NOT.search(d["topic"])), None))


from beom_workbench.core.scaffold import LOCKFILES  # noqa: E402 — 잠금 파일 목록은 한 곳


def _undo_code_before_plan(run: dict, prev: dict) -> list[str]:
    """요청사항 실행의 첫 계획 — 그 전에 만들거나 고친 제품 파일을 시작 전 상태로 되돌린다. Claude 는 편집 문지기(hookgate)가 미리
    막지만 Codex 는 편집을 자기 격리 안에서 바로 해서 툴킷이 미리 막을 길이 없다 — 순서는 모델이 아니라 툴킷이 지킨다.
    툴킷 작업 파일(.beom)과 의존성 설치가 만든 잠금 파일은 둔다."""
    if run.get("mode") != "request" or prev.get("steps") or (prev.get("history") or []):
        return []
    from beom_workbench.core.checkpoints import Checkpoints
    try:
        cp = Checkpoints(inbox.run_dir(settings, run["id"]), run["target_root"])
        cps = cp.list()
        start = next((c for c in reversed(cps) if c.get("label") == "시작 전"), None)
        if not start:
            return []
        files = [f["path"] for f in cp.changes(start["id"])["files"] if Path(f["path"]).name not in LOCKFILES]
        if not files:
            return []
        cp.snapshot("계획 전 코드 되돌리기 전")  # 되돌린 것도 체크포인트에서 다시 꺼낼 수 있게 (사용자가 그 사이 고친 것 포함)
        done = cp.revert_paths(start["id"], files)
    except Exception:  # noqa: BLE001 — 되돌리기가 실패해도 계획은 받는다 (다음 확인이 다시 본다)
        return []
    if done:
        inbox.log(settings, run["id"], "note", tone="warn", text="계획 전에 만든 제품 파일을 되돌렸습니다: " + ", ".join(done[:5]))
    return done


def _shared_rel(run: dict, tool: str, inp: dict) -> str | None:
    if tool not in inbox.FILE_TOOLS:
        return None
    path = inp.get("file_path") or inp.get("notebook_path")
    try:
        return Path(path).resolve().relative_to(Path(run["target_root"]).resolve()).as_posix() if path else None
    except (OSError, ValueError):
        return None


STYLE_EXTS = (".css", ".scss", ".sass", ".less")


def _shared_gate(run: dict, steps: list[dict], changes: list[dict] | None, prev: dict) -> tuple[list[dict], str | None]:
    """기존 화면 쪽 공통 부품을 고치는 계획 — 신고(shared_changes: 파일·이유)가 있어야 한다. 사용자가 계획 카드에서 보고 승인한다."""
    parts = run.get("shared_parts") or {}
    norm = lambda f: _path_part(f).replace("\\", "/").lstrip("./")  # noqa: E731
    root = Path(run["target_root"])
    style = lambda f: f.lower().endswith(STYLE_EXTS) and (root / f).is_file() and bool(run.get("existing"))  # noqa: E731
    if not parts and not any(style(norm(c.get("file", ""))) for c in changes or [] if isinstance(c, dict)):
        return [], None
    if changes is None:
        changes = prev.get("shared_changes") or []  # 다시 낸 계획 — 앞의 신고를 이어 받는다
    out, bad = [], []
    for c in changes or []:
        if not isinstance(c, dict) or not str(c.get("file", "")).strip():
            continue
        f, why = norm(c["file"]), str(c.get("why", "")).strip()
        if f not in parts and not style(f):
            bad.append(f"{f} — 다른 화면들이 쓰는 기존 공통 부품이 아님 (신고 없이 고친다)")
        elif not why:
            bad.append(f"{f} — why(왜 꼭 고쳐야 하는지, 다른 화면에 무엇이 달라지는지)가 비어 있음")
        else:
            out.append({"file": f, "why": why[:300], "users": parts.get(f, 0)})  # 0: 공통 스타일 (쓰는 화면 수를 세지 않는다)
    if bad:
        return [], "shared_changes 를 고친다: " + "; ".join(bad)
    declared = {c["file"] for c in out}
    touch = sorted({norm(f) for s in steps for f in s["files"]} & set(parts) - declared)
    if touch:
        return [], ("계획이 다른 화면들이 쓰는 기존 공통 부품을 고친다: " + ", ".join(f"{f}({parts[f]}곳)" for f in touch[:6])
                    + " — 공통 부품은 고치지 않고 새 화면 쪽에서 해결하도록 steps 를 바꾼다. 꼭 고쳐야 하면 shared_changes=[{file, why}] 로 "
                      "신고한다 (사용자가 계획 카드에서 보고 승인해야 고칠 수 있다)")
    return out, None


def _layout_gate(run: dict, steps: list[dict], basis: str) -> list[str]:
    """새 프로젝트의 폴더 구조 — 계획한 파일과 지금 만든 파일을 언어별 정석 구조와 견준다. 기존 코드가 있던 폴더는 그 구조를 따르므로
    보지 않는다. 사용자가 구조를 정해 주었으면(layout_basis) 보지 않는다. 기획 모드는 코드를 만들지 않으니 보지 않는다."""
    from beom_workbench.core import runner as runner_core, scaffold
    if basis.strip() or run.get("mode") not in ("request", "reference") or not runner_core.new_project(run):
        return []
    planned = [f for s in steps for f in s["files"]]
    return scaffold.check(planned, scaffold.files_now(Path(run["target_root"])))


def _design_gate(run: dict, steps: list[dict], picks: list[dict], look: list[str], basis: str) -> str | None:
    """새로 만드는 화면의 디자인 방향 — 참고 화면을 찍어 보고(조사), 방향이 다른 시안을 그림으로 보여 사용자가 고르게 한다.
    규칙에 '반드시'라고 적어도 같은 에이전트가 어떤 때는 하고 어떤 때는 건너뛰었다 — 계획을 받을 때 확인한다.
    빼는 경우: 화면이 없는 작업, 기존 화면이 여럿 있는 프로젝트(기존 디자인을 따른다), 사용자가 정한 디자인(design_basis),
    화면 엔진이 없는 설치. 참고 화면은 웹 접속이 규칙상 막혔거나 사용자가 거절했으면 뺀다."""
    from beom_workbench.core import conventions, runner as runner_core, screens
    ui = _screen_files(steps)
    design = _design_pick(picks)
    if not (ui or design) or basis.strip() or not screens.available() or not runner_core.ui_task(run):
        return None
    if conventions.has_screens(Path(run["target_root"]), before=_started(run)):
        return None
    problems = []
    evs = [e.get("kind") for e in inbox.events(settings, run["id"])[0]]
    if inbox.decide(run, "web") != "refuse" and not ({"design_refs", "design_refs_declined"} & set(evs)):
        problems.append("참고 화면을 아직 보지 않았다 — mcp__beom-workbench__wb_design_refs 로 같은 분야의 잘 만든 서비스 2~4곳을 찍어 "
                        "보고 (사용자가 거절하면 그대로 진행)")
    shots = (design or {}).get("shots") or {}
    # 다시 낸 계획 — 앞서 시안을 그림으로 보여 줬으면(사용자가 이미 보고 골랐으면) 다시 붙이라고 하지 않는다
    seen_before = max((len(d.get("shots") or {}) for d in ((run.get("plan") or {}).get("decisions") or [])), default=0)
    broken = [x for d in picks for x in d.get("sian_broken") or []]
    if broken:
        problems.append("시안이 휴대폰 화면에서 깨진다 — 고쳐서 다시 낸다: " + " · ".join(broken[:6]))
    if len(shots) < 2 and seen_before < 2:
        problems.append("'디자인 방향' 결정에 방향이 확실히 다른 시안을 선택지 2개 이상 그림으로 붙여야 한다 — 핵심 화면 한 장씩 "
                        ".beom/looks/a.html 처럼 만들고 decisions 의 그 결정에 looks={\"선택지\": \".beom/looks/a.html\", …} "
                        "(선택지 이름은 choice·options 와 똑같이)" + ("" if design else " — 그 결정이 아직 없다"))
    if not problems:
        return None
    return ("새로 만드는 화면의 디자인을 정하기 전에: " + " / ".join(problems) +
            ". 사용자가 브랜드·디자인을 이미 정해 주었으면 design_basis 에 근거를 적는다")


SIAN_BROKEN = ("가로로 넘침", "글자가 잘림", "한두 글자씩 줄바뀜", "누르는 곳끼리 겹침")


def _shoot_looks(run: dict, picks: list[dict]) -> list[str]:
    """디자인 방향 시안(HTML)을 찍어 결정에 붙인다 — {"선택지": ".beom/looks/a.png"}. 시안은 대상 폴더의 .beom 안 HTML 만.
    찍을 때 디자인 점검도 한다 — 사용자는 시안 그림을 보고 고른다. 가로로 넘치거나 글자가 쪼개진 시안은 '방향'이 아니라 깨진 화면이라
    결정에 sian_broken 으로 남겨(계획을 받지 않는다) 고쳐 다시 내게 한다."""
    from beom_workbench.common.response import ToolError
    from beom_workbench.core import screens
    root = Path(run["target_root"]).resolve()
    jobs, errors = [], []
    for d in picks:
        for label, rel in (d.get("looks") or {}).items():
            f = (root / rel).resolve()
            if f.suffix.lower() not in (".html", ".htm") or not f.is_file() or not f.is_relative_to(root / ".beom"):
                errors.append(f"시안은 대상 폴더 .beom/ 안의 HTML 이어야 함: {rel}")
                continue
            jobs.append((d, label, f))
        d.pop("looks", None)
    if not jobs:
        return errors
    if not screens.available():
        return errors + ["이 설치에는 화면 엔진이 없어 시안을 찍지 못함 — 선택지는 글로만 보인다"]
    out_dir = root / ".beom" / "looks"
    out_dir.mkdir(parents=True, exist_ok=True)
    with screens.serve_folder(root) as srv:
        pages = []
        for i, (d, label, f) in enumerate(jobs):
            spec = {"file": f.relative_to(root).as_posix(), "mobile": True, "wait_ms": 600, "audit": True}
            pg = screens.page_spec(spec, None, root, str(out_dir / f"look-{i + 1}.png"), file_base=srv.base)
            pg["full_page"] = False  # 첫 화면 그대로 — 카드 안에서 비교하기 좋게
            pages.append(pg)
        try:
            got = screens.capture(pages)
        except ToolError as e:
            return errors + [e.message]
    for (d, label, f), r in zip(jobs, got):
        if r.get("ok"):
            d.setdefault("shots", {})[label] = Path(r["out"]).resolve().relative_to(root).as_posix()
            fix, _ = _design_findings(r.get("audit")) if r.get("audit") else ([], [])
            broken = [x for x in fix if any(k in x for k in SIAN_BROKEN)]
            if broken:
                d.setdefault("sian_broken", []).extend(f"{f.name} (모바일 390px): {x}" for x in broken[:4])
        else:
            errors.append(f"시안을 찍지 못함: {f.name} {(r.get('errors') or [''])[0]}")
    return errors


@mcp.tool()
def wb_plan_set(steps: list[dict], summary: str = "", design: str = "", decisions: list[dict] | None = None,
                 look: list[str] | None = None, previews: list[str] | None = None, follows: list[dict] | None = None,
                 design_basis: str = "", rules_checked: list[str] | None = None, layout_basis: str = "",
                 shared_changes: list[dict] | None = None) -> dict:
    """구현 계획을 낸다 — 승인을 기다리는 실행이면 사용자가 앱에서 승인한 뒤 구현한다 (끝까지 맡김·계획 승인 끔은 이어서 구현). steps: [{title, files?: [...], commands?: [...], requirements?: ["R1"]}].
    decisions: 사전 설계 결정 [{topic: "데이터 저장", choice: "SQLite 파일", options: ["JSON 파일"], why: "한 줄"}] — 형태(웹·앱·API)·
    데이터 저장·스택·실행 방법·디자인 방향처럼 결과를 크게 가르는 것. 사용자가 승인하며 대안으로 바꿀 수 있다.
    look: 디자인 컨셉 1~2줄, 전문 용어 없이 (사용자용). 색·글꼴 같은 수치는 계획에 쓰지 말고 구현의 토큰 파일에.
    시안: 디자인 방향 결정에 looks={"선택지 이름": ".beom/looks/a.html", …} 를 주면 툴킷이 각 시안(핵심 화면 한 장)을 찍어 계획
    카드의 선택지 옆에 그림으로 보여 준다 — 사용자가 보고 고른다. 선택지 이름은 choice·options 와 똑같이.
    design·previews 는 예전 형식 — 앱이 보여 주지 않는다.
    follows: 기존 화면이 있는 프로젝트에서 화면을 만들·고칠 때 — 다른 화면들에서 확인한 규칙과 근거
    [{rule: "목록에서 상세는 행 더블클릭", evidence: ["src/pages/OrderList.jsx:8", "src/pages/UserList.jsx:8"]}]. 근거는 지금 있는
    코드의 파일:줄이어야 하고 (툴킷이 확인한다), 규칙 전체에 걸쳐 서로 다른 기존 파일 2개 이상. 사용자가 계획 카드에서 본다.
    rules_checked: 지시의 [작업 규칙·정책] 에 나온 문서 중 읽은 것 ["../../AGENTS.md", "docs/conventions.md"] — 에이전트 지시·규칙
    허브는 모두, 정책·가이드 문서는 이번 작업에 닿는 것을 (툴킷이 확인한다). 그 문서에서 따를 조항은 follows 의 근거(문서:줄)로.
    새로 만드는 화면이면 '디자인 방향' 결정에 시안(looks)을 선택지 2개 이상 붙여야 하고, 그 전에 참고 화면(wb_design_refs)을
    찍어 봐야 한다 — 툴킷이 확인한다. 사용자가 브랜드·디자인을 정해 주었으면 design_basis 에 그 근거(요청·첨부의 어디)를 적는다.
    shared_changes: 다른 화면들이 쓰는 기존 공통 부품([고치지 않을 공통 부품] 줄)을 꼭 고쳐야 할 때만 [{file: "src/components/BaseGrid.vue",
    why: "목록에 체크칸 이름을 붙이려고 — 다른 화면 7곳도 함께 바뀜"}]. 사용자가 계획 카드에서 보고 승인해야 그 파일을 고칠 수 있다.
    새 프로젝트(빈 폴더)면 steps.files 에 설정·테스트 파일까지 적는다 — 툴킷이 언어별 정석 구조와 견주어 분명히 벗어난 곳을 알린다.
    사용자가 구조·파일 이름을 정해 주어 다르게 하면 layout_basis 에 그 근거(요청의 어느 말)를 적는다 (계획 카드에 보인다).
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
        item = {"topic": str(d["topic"]).strip()[:60], "choice": choice, "options": opts, "why": str(d.get("why", "")).strip()[:300]}
        if isinstance(d.get("looks"), dict):
            item["looks"] = {str(k).strip()[:120]: str(v) for k, v in d["looks"].items() if str(k).strip()[:120] in [choice, *opts]}
        picks.append(item)

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
    prev = (run or {}).get("plan") or {}
    if rules_checked is None and prev.get("rules_checked"):
        rules_checked = list(prev["rules_checked"])
    old_design = _design_pick(prev.get("decisions") or [])
    new_design = _design_pick(picks)
    if old_design and new_design and not new_design.get("looks") and old_design.get("shots"):
        keep = [new_design["choice"], *new_design["options"]]
        carried = {k: v for k, v in old_design["shots"].items() if k in keep}
        if carried:
            new_design["shots"] = carried
    rules, rule_errors = _follows(run, clean, follows)
    if rule_errors and rules is None:
        return {"ok": False, "error": rule_errors[0], "problems": rule_errors[1:]}
    read, read_error = _rules_checked(run, rules_checked, rules)
    if read_error:
        return {"ok": False, "error": read_error}
    look_errors = _shoot_looks(run, picks) if run is not None else []
    gate = _design_gate(run, clean, picks, lines, design_basis) if run is not None else None
    if gate:
        return {"ok": False, "error": gate, **({"look_errors": look_errors} if look_errors else {})}
    if layout_basis.strip() == "" and prev.get("layout_basis") and run is not None:
        layout_basis = prev["layout_basis"]  # 다시 낸 계획 — 앞에서 남긴 구조 근거를 이어 받는다
    shared, shared_error = _shared_gate(run, clean, shared_changes, prev) if run is not None else ([], None)
    if shared_error:
        return {"ok": False, "error": shared_error}
    layout = _layout_gate(run, clean, layout_basis) if run is not None else []
    if layout:
        return {"ok": False, "error": "새 프로젝트인데 정석 폴더 구조와 다른 곳이 있음: " + layout[0],
                "problems": layout[1:],
                "recovery": "그 언어의 정석 구조로 steps.files 를 고친다 (설정·테스트 파일까지). 사용자가 구조를 정해 주어 다르게 하면 "
                            "layout_basis 에 근거(요청의 어느 말)를 적는다"}
    for d in picks:  # 깨진 시안 표시는 확인용 — 계획에는 남기지 않는다
        d.pop("sian_broken", None)

    def put(r):
        old = r.get("plan") or {}
        history = (old.get("history") or []) + ([{"steps": old["steps"], "summary": old.get("summary")}] if old.get("steps") else [])
        # 계획을 승인하며 결정을 바꾼 바로 그 라운드에 결정에 맞춰 다시 낸 계획은 사용자 결정의 결과다 — 다시 묻지 않는다
        keep = bool(old.get("approved")) and r.get("auto_approve_round") == r.get("round")
        r["plan"] = {"steps": clean, "summary": summary.strip()[:1000], "approved": keep, "history": history[-5:],
                     **({"approved_via": "decision-change"} if keep else {}),
                     "at": inbox._now(), **({"design": design.strip()[:6000]} if design.strip() else {}),
                     **({"decisions": picks[:10]} if picks else {}), **({"look": lines} if lines else {}),
                     **({"previews": shots} if shots else {}), **({"follows": rules} if rules else {}),
                     **({"design_basis": design_basis.strip()[:300]} if design_basis.strip() else {}),
                     **({"layout_basis": layout_basis.strip()[:300]} if layout_basis.strip() else {}),
                     **({"shared_changes": shared} if shared else {}),
                     **({"rules_checked": read} if read else {})}
    out = _update(put)
    early = _undo_code_before_plan(run, prev) if run is not None and out.get("ok") else []
    if out["ok"] and shared and run is not None:
        from beom_workbench.core import runner as runner_core
        runner_core.unlock_shared(settings, run, only=set(runner_core.shared_parts_unlocked(inbox.load_run(settings, run["id"]))))
    if early:
        out["reverted_before_plan"] = early
        waits_now = run.get("plan_approval", True) and run.get("confirm_at", "start") != "end"
        out["warning"] = ("계획 전에 만든 제품 파일을 시작 전 상태로 되돌렸다 (" + ", ".join(early[:6]) + (" …" if len(early) > 6 else "")
                          + ") — 순서는 합격 케이스 → 계획 → (승인) → 구현이다. "
                          + ("사용자가 승인한 뒤 계획대로 다시 만든다" if waits_now else "계획대로 다시 만든다"))
    if out["ok"]:
        inbox.log(settings, os.environ["BEOM_WORKBENCH_RUN"], "plan", steps=len(clean))
        if look_errors:
            out["look_errors"] = look_errors
        if rule_errors:
            out["follows_errors"] = rule_errors
    if bad:
        out["warning"] = ((out["warning"] + " / ") if out.get("warning") else "") + \
            "미리보기로 쓸 수 없는 경로(대상 폴더 .beom/ 안 png 만): " + ", ".join(bad)
    waits = run is None or (run.get("plan_approval", True) and run.get("confirm_at", "start") != "end")
    return {**out, "note": "사용자 승인 전에는 코드를 바꾸지 않는다" if waits
            else "이 실행은 계획 승인을 기다리지 않는다 — 이어서 구현·검증한다 (사용자는 마지막에 확인한다)"}


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

        from beom_workbench.core import tasks
        # 사용자에게 넘기는 기획 폴더는 정해진 결과물만 — 작성용 스크립트·따로 만든 PDF·HTML·문서는 툴킷이 옮긴다
        tidied = brief.tidy(run["target_root"], existed=lambda rel: tasks._existed(settings, run, rel))
        if tidied:
            inbox.log(settings, run["id"], "brief_tidy", moved=tidied[:20])
        revising = _revision_pass(run)
        if revising and run.get("brief_changes_round") != run.get("round"):
            # 고칠 점을 받은 라운드 — 바꾼 값이 다른 문서·예시·시나리오에 남지 않았는지 툴킷이 찾아 본 뒤에 인계한다
            return {"ok": False, "error": "고칠 점을 반영한 라운드 — wb_brief_check(changes=[{\"old\": 옛 값, \"new\": 새 값}]) 로 "
                                          "옛 값이 남은 곳을 확인한 뒤 인계한다 (바꾼 값 없이 기능만 더했으면 changes=[])"}
        chk = brief.check(run["target_root"], _policy_docs(run), run.get("brief_changes") if revising else None)
        if not chk["ok"]:
            return {"ok": False, "error": "기획 패키지가 아직 맞물리지 않음 — 고친 뒤 다시 인계한다", "problems": chk["problems"]}
    if run is not None and run.get("port_id") and run.get("project_id"):
        pending = _pending_approvals(run)
        if pending:  # 승인 뒤에 쓰면 '승인이 남았다'는 지난 내용이 인수인계에 남는다 — 무엇을 확인받는지 말하고 라운드를 끝낸다
            return {"ok": False, "error": f"사용자 확인이 {pending}건 남아 있음 — 인수인계는 승인 뒤에 쓴다. 무엇을 확인받는지 말하고 라운드를 끝낸다"}
        notice = _license_notice(run)
        if notice:  # 허용형 원본도 저작권 표기는 남겨야 한다 — 빠뜨린 채 인계하지 않게
            return {"ok": False, "error": "원본 저작권 표기가 대상에 없음 — 남긴 뒤 다시 인계한다", **notice}
    finish = _finish_checks(run) if run is not None else []
    if finish:  # 계획 때는 계획한 파일만 보았다 — 다 만든 실제 파일로 정석 구조·버전 고정 파일을 다시 본다
        return {"ok": False, "error": "인계 전에 고칠 것: " + finish[0], "problems": finish[1:]}
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


def _finish_checks(run: dict) -> list[str]:
    """새 프로젝트를 넘기기 전 — 실제 파일이 그 언어의 정석 구조인지(사용자가 구조를 정했으면 빼고), Node 의존성이 있으면 버전을
    고정하는 잠금 파일이 있고 커밋에서 빠지지 않는지. 이미 있던 프로젝트는 그 프로젝트의 방식을 따르므로 보지 않는다."""
    from beom_workbench.core import deps, layout, runner as runner_core, scaffold
    if run.get("mode") not in ("request", "reference") or not runner_core.new_project(run):
        return []
    root = Path(run["target_root"])
    # 넘길 때는 깨지는 것(설정 파일 없음 등)만 막는다 — 배치·테스트 위치 같은 정리 규칙은 계획 때 알렸다
    out = [] if (run.get("plan") or {}).get("layout_basis") else scaffold.check([], scaffold.files_now(root), hard_only=True)
    at = layout.root_for(root, "node")
    pj = at / "package.json"
    try:
        pkg = json.loads(pj.read_text(encoding="utf-8")) if pj.is_file() else {}
    except (OSError, ValueError):
        pkg = {}
    if isinstance(pkg, dict) and (pkg.get("dependencies") or pkg.get("devDependencies")):
        from beom_workbench.core.scaffold import NODE_LOCKS
        homes = [at, deps._npm_lockfile(at).parent]
        locks = [h / n for h in dict.fromkeys(homes) for n in NODE_LOCKS if (h / n).is_file()]
        if not locks:
            out.append("의존성 버전을 고정하는 잠금 파일(package-lock.json 등)이 없음 — wb_deps 로 설치하면 만들어진다 (설치·테스트를 "
                       "셸로 돌렸으면 wb_deps·wb_task 로 다시)")
        elif all(_ignored(root, x) for x in locks):
            out.append(f"{locks[0].name} 이(가) .gitignore 에 걸려 커밋되지 않음 — 버전을 고정하는 파일이라 .gitignore 에서 뺀다")
    return out


def _ignored(root: Path, f: Path) -> bool:
    """git 이 무시하는 파일인가 — 저장소면 git 에 묻고, 아니면 .gitignore 의 줄을 이름으로 견준다."""
    import fnmatch
    import subprocess
    try:
        r = subprocess.run(["git", "check-ignore", "-q", str(f)], cwd=root, capture_output=True, timeout=20)
        if r.returncode in (0, 1):
            return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        pass
    gi = root / ".gitignore"
    try:
        lines = [x.strip().strip("/") for x in gi.read_text(encoding="utf-8").splitlines() if x.strip() and not x.startswith("#")]
    except OSError:
        return False
    ignored = False
    rel = f.resolve().relative_to(root.resolve()).as_posix() if f.resolve().is_relative_to(root.resolve()) else f.name
    for pat in lines:  # git 처럼 뒤의 줄이 앞의 줄을 덮는다
        neg = pat.startswith("!")
        pat = pat[1:] if neg else pat
        pat = pat[3:] if pat.startswith("**/") else pat
        if fnmatch.fnmatch(f.name, pat) or fnmatch.fnmatch(rel, pat):
            ignored = not neg
    return ignored


def _pending_approvals(run: dict) -> int:
    from beom_workbench.core import port, project
    try:
        conn, _ = project.load(settings, run["project_id"])
        return int((port.status_(conn, run["port_id"], brief=True).get("approvals") or {}).get("pending") or 0)
    except Exception:  # noqa: BLE001 — 원장을 못 읽으면 막지 않는다
        return 0


def _license_notice(run: dict) -> dict | None:
    from beom_workbench.core import port, project
    try:
        conn, _ = project.load(settings, run["project_id"])
        return port.status_(conn, run["port_id"], brief=True).get("license_notice")
    except Exception:  # noqa: BLE001 — 원장을 못 읽으면 막지 않는다 (상태 화면이 따로 알린다)
        return None


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
def wb_brief_check(changes: list[dict] | None = None) -> dict:
    """기획 패키지 점검 — .beom/brief/ 의 기획서·정책서·화면설계·테스트시나리오가 서로 맞물리는지 본다:
    빠진 문서·절, 테스트 시나리오가 없는 요구사항(R), 와이어프레임이 없는 화면(S), 스크린샷 안 찍은 와이어프레임, 열린 질문 수.
    problems 가 비어야 인계(wb_handoff)할 수 있다. notes 는 참고.
    changes: 사용자가 보낸 고칠 점으로 바꾼 값 — [{"old": "1시간", "new": "2시간", "keep": ["취소 마감"]}]. 툴킷이 문서·와이어프레임
    전체에서 옛 값이 남은 줄을 찾는다 (예시·경계값·시나리오에 남기 쉽다). 남은 줄은 고치거나, 일부러 두면 그 줄의 말을 keep 에
    넣는다. 고칠 점을 받은 라운드에서는 인계 전에 꼭 부른다 — 바꾼 값이 없으면(기능만 더함) changes=[]."""
    from beom_workbench.core import brief

    run = _run()
    if run is None:
        return {"ok": False, "error": "자동 실행 밖"}
    if changes is not None:
        clean = [{"old": str(c.get("old") or ""), "new": str(c.get("new") or ""), "keep": [str(k) for k in c.get("keep") or []]}
                 for c in changes if isinstance(c, dict)]
        inbox.update_run(settings, run["id"], lambda r: r.update(brief_changes=clean, brief_changes_round=r.get("round")))
        return brief.check(run["target_root"], _policy_docs(run), clean)
    return brief.check(run["target_root"], _policy_docs(run), run.get("brief_changes") if _revision_pass(run) else None)


def _revision_pass(run: dict) -> bool:
    """인계까지 끝난 기획에 고칠 점이 와서 도는 라운드인가."""
    return bool(run.get("revision")) and bool(run.get("revision_round")) and run.get("revision_round") == run.get("round")


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
    if app or url:
        from beom_workbench.core import deps
        deps.ensure(settings, run, Path(run["target_root"]))  # 앱·서버가 쓰는 의존성을 먼저 (바뀐 것이 없으면 바로 지나간다)
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


def _design_findings(audit: dict | None) -> tuple[list[str], list[str]]:
    """점검 결과 → (고칠 것, 참고). 고칠 것: WCAG 대비 미달·24px 미만 누르는 곳·가로 넘침·잘린 글자·겹친 버튼·12px 미만 글자."""
    if not audit:
        return ["점검 결과를 읽지 못함 (페이지가 이동 중이었을 수 있음)"], []
    iss, var = audit.get("issues") or {}, audit.get("variety") or {}
    fix, advice = [], []
    for c in iss.get("contrast") or []:
        fix.append(f"대비 부족 {c['ratio']}:1 (필요 {c['need']}:1) — {c['el']} '{c['text']}'")
    for t in iss.get("tiny_targets") or []:
        fix.append(f"누르기 너무 작음 {t['w']}×{t['h']}px (24px 이상) — {t['el']} '{t['text']}'")
    if iss.get("overflow"):
        o = iss["overflow"]
        fix.append(f"가로로 넘침 — 페이지 {o['page']}px > 화면 {o['viewport']}px ({', '.join(o.get('wide') or [])})")
    for c in iss.get("clipped") or []:
        fix.append(f"글자가 잘림 — {c['el']} '{c['text']}'")
    for c in iss.get("broken_wrap") or []:
        fix.append(f"글자가 한두 글자씩 줄바뀜 ({c['lines']}줄) — 칸을 넓히거나 word-break: keep-all — {c['el']} '{c['text']}'")
    for o in iss.get("overlap") or []:
        fix.append(f"누르는 곳끼리 겹침 — {o['a']} · {o['b']}")
    for t in iss.get("small_text") or []:
        fix.append(f"글자가 너무 작음 {t['size']}px (12px 이상) — {t['el']} '{t['text']}'")
    for t in iss.get("small_targets") or []:
        advice.append(f"모바일에서 누르기 작음 {t['w']}×{t['h']}px (44px 권장) — {t['el']} '{t['text']}'")
    if len(var.get("fonts") or []) > 3:
        advice.append(f"글꼴이 {len(var['fonts'])}가지 — 제목·본문 2가지(많아야 3)로: {', '.join(var['fonts'][:6])}")
    if len(var.get("sizes") or []) > 8:
        advice.append(f"글자 크기가 {len(var['sizes'])}단계 — 6~8단계로 줄이면 위계가 또렷해진다: {var['sizes']}")
    if (var.get("text_colors") or 0) > 10:
        advice.append(f"글자 색이 {var['text_colors']}가지 — 토큰의 몇 가지 색으로 모은다")
    return fix, advice


@mcp.tool()
def wb_ui_conventions(near: list[str] | None = None, like: str = "") -> dict:
    """기존 화면의 규칙 — 이 프로젝트의 다른 화면들이 실제로 어떻게 만들어졌는지 코드에서 뽑는다 (규칙은 툴킷이 정하지 않는다).
    shared: 여러 화면이 함께 쓰는 부품(공통 컴포넌트·공통 함수·그리드 같은 위젯)과 쓰는 방식 — 화면/팝업 안을 나눠 이벤트(예:
    onRowDoubleClick 4/4 → 목록에서 상세는 행 더블클릭)와 옵션(예: 팝업 안 그리드 height=320 3/3)이 몇 곳에서 쓰였는지, 근거(파일:줄).
    events: 프로젝트 전체의 이벤트 이름. library: 공통 함수 파일의 다른 함수들 — 지금 한 화면만 쓰거나 아무도 안 쓰는 것도
    이미 있는 공통 기능이다 (엑셀·공통코드·기간 검사 …). 새로 만들기 전에 여기서 찾는다.
    similar: 지금 만들·고칠 화면과 비슷한 기존 화면 — near=[만들·고칠 파일], like="목록 상세" 처럼.
    비슷한 화면을 열어 읽고, 같은 부품을 같은 방식으로 쓴다. 계획(wb_plan_set)의 follows 에 따를 규칙과 근거를 남긴다."""
    from beom_workbench.core import conventions

    run = _run()
    if run is None:
        return {"ok": False, "error": "자동 실행 밖"}
    res = conventions.scan(Path(run["target_root"]), near=near, like=like)
    if not res["shared"] and not res["events"]:
        return {"ok": True, **res, "note": "기존 화면 코드가 없음 — 새로 만드는 화면이면 '화면 디자인' 절대로"}
    return {"ok": True, **res, "next": "similar 의 화면들을 열어 읽고 follows=[{rule, evidence:[파일:줄…]}] 로 계획에 남긴다"}


@mcp.tool()
def wb_design_check(files: list[str] | None = None, url: str | None = None, paths: list[str] | None = None,
                    cmd: str | None = None, actions: list[dict] | None = None) -> dict:
    """디자인 점검 — 화면을 실제 브라우저 엔진으로 모바일(390px)·PC(1280px) 두 크기로 열어, 계산된 스타일과 위치로 잰다:
    글자와 바탕의 대비(WCAG 4.5:1, 큰 글자 3:1), 누르는 곳 크기(24px 미만은 고칠 것, 모바일 44px 권장), 가로 넘침, 잘린 글자,
    겹친 버튼, 12px 미만 글자, 글꼴·글자 크기·색이 너무 많은지. fix 는 고칠 것, advice 는 참고. 스크린샷도 함께 남긴다.
    files=["index.html"] 또는 url·paths·cmd (wb_screen_shot 과 같다), actions 로 눌러 본 뒤의 화면도."""
    from beom_workbench.core import screens

    run = _run()
    if run is None:
        return {"ok": False, "error": "자동 실행 밖"}
    if not screens.available():
        return {"ok": False, "error": "이 설치에는 화면 엔진(PySide6)이 없음"}
    root = Path(run["target_root"]).resolve()
    out_dir = root / ".beom" / "shots"
    out_dir.mkdir(parents=True, exist_ok=True)
    targets: list[tuple[str, dict]] = []
    server = None
    before = None
    from beom_workbench.core import port
    srv = None
    try:
        if url:
            if cmd:
                inbox.gate_commands(settings, {"screen_server": cmd})
            cmd, url = port.fill_port(cmd, url)
            port.check_url(url)
            before = port._files(root)
            if cmd:
                server = port._Server(port._Side("target", root, root / ".beom"), cmd, url)
            for pth in (paths or ["/"])[:6]:
                targets.append(("/" + str(pth).lstrip("/"), {"path": "/" + str(pth).lstrip("/")}))
            base = url
        else:
            if not files:
                return {"ok": False, "error": "files(HTML) 또는 url(서버 주소)이 필요함"}
            for f in files[:6]:
                p = (root / f).resolve()
                if not p.is_relative_to(root) or not p.is_file():
                    return {"ok": False, "error": f"대상 폴더 안의 파일이 아님: {f}"}
                targets.append((p.relative_to(root).as_posix(), {"file": p.relative_to(root).as_posix()}))
            srv = screens.serve_folder(root)
            srv.__enter__()
            base = None
        pages = []
        for label, spec in targets:
            for mobile in (True, False):
                name = "check-" + re.sub(r"[^\w-]+", "_", label).strip("_")[:50] + ("-mobile" if mobile else "")
                pages.append((label, mobile, screens.page_spec({**spec, "mobile": mobile, "wait_ms": 700, "audit": True,
                                                                "actions": list(actions or [])},
                                                               base, root, str(out_dir / f"{name}.png"),
                                                               file_base=srv.base if srv else None)))
        got = screens.capture([pg for _, _, pg in pages])
    finally:
        if server:
            server.close()
        if srv:
            srv.__exit__(None, None, None)
        if before is not None:
            port._drop_new(root, before, keep=set())
    report = []
    for (label, mobile, _pg), r in zip(pages, got):
        fix, advice = _design_findings(r.get("audit"))
        report.append({"page": label, "size": "모바일 390px" if mobile else "PC 1280px", "shot": r.get("out") if r.get("ok") else None,
                       "fix": fix[:20], "advice": advice[:10], **({"errors": r["errors"][:3]} if r.get("errors") else {})})
    inbox.log(settings, run["id"], "screen_shots", shots=[x["shot"] for x in report if x["shot"]])
    total = sum(len(x["fix"]) for x in report)
    return {"ok": True, "fix_count": total, "pages": report,
            "next": (("fix 를 고친 뒤 다시 점검한다 — 대비는 토큰의 색을, 크기는 패딩·최소 높이를 고친다" if not run.get("existing") else
                      "fix 중 새로 만든 화면 파일·새 클래스에서 오는 것은 고치고 다시 점검한다. 원래 있던 디자인 토큰·공통 부품·전역 "
                      "스타일에서 오는 것은 고치지 않고 인수인계 follow_ups 에 남긴다 (고치면 다른 화면이 모두 바뀐다)")
                     if total else "고칠 것 없음 — advice 를 보고 다듬는다")}


@mcp.tool()
def wb_design_refs(urls: list[str], mobile: bool = False) -> dict:
    """참고 화면 찍기 — 같은 분야에서 디자인으로 좋은 평을 받는 서비스의 실제 화면을 찍어 .beom/refs/ 에 남긴다 (최대 4개).
    결과의 shot 을 직접 열어 보고 색 분위기·글꼴·사진/그림 쓰는 법·배치·기억에 남는 요소를 뽑는다 — 베끼지 않고 원리만.
    바깥 사이트에 접속하는 일이라 웹 권한 규칙을 따른다 (묻기면 사용자에게 한 번 묻는다). 화면은 쿠키·저장소 없이 새로 연다."""
    from beom_workbench.common.response import ToolError
    from beom_workbench.core import screens

    run = _run()
    if run is None:
        return {"ok": False, "error": "자동 실행 밖"}
    picks = [u for u in (urls or [])[:4] if re.match(r"^https?://[^\s/]+", str(u))]
    if not picks:
        return {"ok": False, "error": "urls 에 http(s) 주소를 1~4개"}
    if not screens.available():
        return {"ok": False, "error": "이 설치에는 화면 엔진(PySide6)이 없음 — 참고 서비스는 웹 검색 결과의 설명으로만 본다"}
    decision = inbox.decide(run, "web")
    if decision == "refuse":
        return {"ok": False, "error": "규칙상 웹 접속이 허용되지 않음 — 아는 범위에서 참고 서비스를 적는다"}
    if decision == "ask":
        ans = inbox.ask(settings, run["id"], "permission", {
            "summary": "참고 화면 찍기 (웹 접속): " + ", ".join(picks), "tool": "mcp:wb_design_refs", "input": {"urls": picks},
            "permission_kind": "web"})
        if not ans.get("allow"):
            inbox.log(settings, run["id"], "design_refs_declined", urls=picks)
            return {"ok": False, "error": "사용자가 참고 화면 찍기를 거절함 — 아는 범위에서 참고 서비스를 적는다"}
        if ans.get("always"):
            inbox.update_run(settings, run["id"], lambda r: r["policy"].__setitem__("web", "allow"))
    out_dir = Path(run["target_root"]).resolve() / ".beom" / "refs"
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = []
    for u in picks:
        name = re.sub(r"[^\w-]+", "_", re.sub(r"^https?://", "", u)).strip("_")[:60] + ("-mobile" if mobile else "")
        w, h = (390, 844) if mobile else (1280, 900)
        pages.append({"url": u, "out": str(out_dir / f"{name}.png"), "width": w, "height": h, "wait_ms": 2500, "full_page": False,
                      "timeout_ms": 30000})
    try:
        got = screens.capture(pages)
    except ToolError as e:
        return {"ok": False, "error": e.message}
    shots = [{"url": u, "ok": bool(r.get("ok")), "shot": r.get("out") if r.get("ok") else None, "title": r.get("title"),
              "errors": (r.get("errors") or [])[:2]} for u, r in zip(picks, got)]
    inbox.log(settings, run["id"], "design_refs", shots=[s["shot"] for s in shots if s["shot"]])
    return {"ok": True, "shots": shots}


def _url_shot(run: dict, url: str, paths: list[str], cmd: str | None, mobile: bool, width, height, actions) -> dict:
    """서버 화면 — cmd 가 있으면 검증과 같은 방식으로 잠깐 띄워 찍고 끈다. 그동안 프로그램이 만든 파일(데이터 등)은 치운다."""
    from beom_workbench.core import port, screens
    if not screens.available():
        return {"ok": False, "error": "이 설치에는 화면 엔진(PySide6)이 없음"}
    if cmd:  # 포트를 채우기 전의 명령으로 묻는다 — 채운 뒤에 물으면 찍을 때마다 포트가 달라 같은 명령을 매번 다시 묻는다
        inbox.gate_commands(settings, {"screen_server": cmd})
    cmd, url = port.fill_port(cmd, url)
    port.check_url(url)
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
