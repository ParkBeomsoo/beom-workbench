"""자동 실행용 도구 — 실행기가 띄운 Claude Code 에서만 쓰인다(BEOM_WORKBENCH_RUN). 권한 판단과 사람에게 묻기."""
from __future__ import annotations

import json
import os
import re
import sys
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


def _record_refusal(fn):
    """툴킷이 에이전트의 요청을 돌려보낸 까닭을 실행 기록에 남긴다 — 무엇에 몇 번 막혔는지 보여야 규칙·안내를 고칠 수 있다
    (실험: 계획을 다섯 번 돌려보냈는데 까닭이 어디에도 남지 않아 무엇이 문제였는지 알 수 없었다)."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        out = fn(*args, **kwargs)
        rid = os.environ.get("BEOM_WORKBENCH_RUN")
        if rid and isinstance(out, dict) and out.get("ok") is False and out.get("error"):
            try:
                inbox.log(settings, rid, "refused", tool=fn.__name__, error=str(out["error"])[:2000])
            except Exception:  # noqa: BLE001 — 기록을 못 남겨도 답은 그대로
                pass
        return out
    return wrapper


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
    # 격리(macOS·Linux 의 Claude 자체 샌드박스) 안에서 막힌 명령을 Claude 가 '격리 없이' 다시 요청했다 — 격리 밖 실행 허락을 함께 본다
    return approve_action(tool_name, input, escalation=bool((input or {}).get("dangerouslyDisableSandbox")))


def approve_action(tool_name: str, input: dict, escalation: bool = False) -> str:  # noqa: A002
    """에이전트가 하려는 일 하나의 허락 — Claude 의 권한 요청과 Codex 의 승인 요청(app-server)이 같은 판단을 거친다.
    escalation: 에이전트 자신의 격리 밖에서 돌리겠다는 요청 (Claude 의 dangerouslyDisableSandbox, Codex 의 권한 상승) —
    명령 종류의 규칙에 더해 '격리 없이 실행' 규칙을 거치고, 허락하지 않으면 거절한다 (격리 안에서 이미 막힌 명령이다)."""
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
    redirect = (_native_redirect(tool_name, input or {}, decision) if native
                else _redirect(run, tool_name, input or {}, kind, decision))
    if redirect:
        inbox.log(settings, run["id"], "permission", tool=tool_name, permission_kind=kind, decision="redirect",
                  what=mask_command(str((input or {}).get("command"))[:160]))
        return json.dumps({"behavior": "deny", "message": redirect}, ensure_ascii=False)
    late = _late_answer(run, tool_name, input or {}, root)
    if late == "refuse":  # 끊긴 라운드의 카드에 사용자가 늦게 '거절'로 답했다 — 같은 일을 다시 묻지 않는다
        inbox.log(settings, run["id"], "permission", tool=tool_name, permission_kind=kind, decision="refuse", why="late_answer")
        return json.dumps({"behavior": "deny", "message": "사용자가 앞서 이 일을 거절했다 — 다시 시도하거나 묻지 말고, 다른 방법으로 하거나 "
                                                          "하지 못한 것을 인수인계에 적는다."}, ensure_ascii=False)
    if late == "allow" and decision == "ask":
        decision = "allow"  # 끊긴 라운드의 카드에 늦게 '허용'으로 답했다 — 같은 일은 묻지 않는다
    if decision == "ask" and tool_name == "Bash" and inbox.planned_command(run, (input or {}).get("command", "")):
        decision = "allow"  # 사람이 승인한 계획에 그대로 있던 명령 — 다시 묻지 않는다
    shell_cmd = str((input or {}).get("command") or "") if tool_name in ("Bash", "PowerShell") else ""
    rule = inbox.command_rule(shell_cmd, root) if shell_cmd and kind == "commands" else None
    if decision == "ask" and rule and inbox.matches_rule(run, shell_cmd, root):
        decision = "allow"  # 사람이 '이 명령은 항상'으로 허락한 꼴
    from beom_workbench.core import sandbox
    wrap = tool_name == "PowerShell" and run.get("agent", "claude") == "claude" and sandbox.wraps_powershell(run) \
        and bool((input or {}).get("command"))
    # Windows 격리 안에서 돌지 않는 명령(Node·Maven·Gradle·.NET·Go·Cargo·Python 3.12.4+) — 격리 없이 돌릴지는 별도 종류(unsandboxed)의
    # 규칙으로 정한다. 툴킷이 돌리는 명령과 같은 기준 (execenv) — Node 만 보던 탓에 dotnet test·go test 가 격리 안에서 '접근 거부'로 멈췄다
    from beom_workbench.core import execenv
    why_out = execenv.outside_reason(str((input or {}).get("command")), execenv.command_env(root)) if wrap else None
    if escalation:
        why_out = why_out or "escalation"
    node = bool(why_out)
    # 기록에 남는 글은 비밀을 가린다 (카드에는 사람이 판단할 수 있게 원래 입력을 그대로 보여 준다)
    summary = mask_command(f"{inbox.action_label(kind, tool_name, input or {}, root, inbox.read_roots(run, settings))}"
                           f"{f' — 이 파일을 쓰는 다른 화면 {users}곳도 함께 바뀝니다' if users else ''}: "
                           f"{tool_name} {_readable(input, root)[:300]}")
    what = mask_command(str((input or {}).get("command") or (input or {}).get("file_path") or (input or {}).get("url") or "")[:160])
    inbox.log(settings, run["id"], "permission", tool=tool_name, permission_kind=kind, decision=decision, what=what)
    from beom_workbench.core import tasks as _tasks
    approved = run.get("approved_cmds") or []
    # 툴킷이 이 프로젝트에서 격리 밖 실행을 한 번 허락받았으면(wb_task·검증) 같은 허락 — 명령마다 다시 묻지 않는다
    outside_ok = node and (inbox.decide(run, "unsandboxed") == "allow" or "outside" in approved or "outside:node" in approved)
    asked_user = decision == "ask"
    if decision == "ask":
        note = f" — 격리 없이 실행됩니다 ({_tasks.OUTSIDE_WHY.get(why_out, '')})" if node and not outside_ok else ""
        ans = inbox.ask(settings, run["id"], "permission", {"summary": summary + note, "tool": tool_name, "input": input,
                                                            "permission_kind": kind, **({"rule": rule} if rule else {})})
        if ans.get("always") and ans.get("allow"):
            inbox.allow_always(settings, run["id"], kind)
        decision = "allow" if ans.get("allow") else "refuse"
        if kind == "shared" and decision == "allow" and (rel_ok := _shared_rel(run, tool_name, input or {})):
            # 사용자가 이 공통 부품의 수정을 허락했다 — 같은 파일을 고칠 때마다 다시 묻지 않는다
            inbox.update_run(settings, run["id"], lambda r: r.update(
                shared_approved=sorted(set(r.get("shared_approved") or []) | {rel_ok})))
        outside_ok = outside_ok or (node and decision == "allow")  # 사람이 격리 없이 실행된다는 것을 보고 허용했다
    elif decision == "allow" and node and not outside_ok and inbox.decide(run, "unsandboxed") == "ask":
        # 빌드처럼 바로 허용되는 명령이라도 격리 밖으로 나가는 것은 사람이 정한다 (한 번만 묻게 '이 프로젝트에서 항상'을 고를 수 있다)
        ans = inbox.ask(settings, run["id"], "permission", {
            "summary": mask_command(f"{inbox.KINDS['unsandboxed']}: {tool_name} {_readable(input, root)[:300]}"),
            "tool": tool_name, "input": input, "permission_kind": "unsandboxed"})
        if ans.get("always") and ans.get("allow"):
            inbox.allow_always(settings, run["id"], "unsandboxed")
        outside_ok = bool(ans.get("allow"))  # 거절하면 격리 안에서 그대로 돌린다 (Node 도구는 실패할 수 있다)
        asked_user = True
    if decision == "allow" and escalation and not outside_ok:
        # 격리 안에서 이미 막힌 명령의 '격리 밖' 요청 — 격리 밖 실행을 허락하지 않았으니 거절한다 (안에서 다시 돌려도 같은 결과)
        inbox.log(settings, run["id"], "permission", tool=tool_name, permission_kind="unsandboxed", decision="refuse", what=what)
        return json.dumps({"behavior": "deny", "message": ("사용자가 " if asked_user else "이 실행의 권한 규칙이 ")
                           + "격리 밖 실행을 허락하지 않았다. 격리 안에서 할 수 있는 방법을 쓰거나 (테스트·빌드는 wb_task, 의존성 "
                             "설치는 wb_deps 가 맡는다), 하지 못한 것을 인수인계에 적는다."}, ensure_ascii=False)
    if decision == "allow":
        if wrap and not outside_ok:
            # 격리 실행 — 허용한 명령을 Windows 샌드박스 안에서 (대상 폴더·임시·도구 캐시 밖에는 못 쓴다)
            input = {**input, "command": sandbox.wrap_powershell(input["command"], run)}  # noqa: A001
        elif wrap:
            inbox.log(settings, run["id"], "permission", tool=tool_name, permission_kind="unsandboxed", decision="allow", what=what)
        return json.dumps({"behavior": "allow", "updatedInput": input}, ensure_ascii=False)
    if kind == "shared":
        rel = _shared_rel(run, tool_name, input or {})
        if rel and decision == "refuse":
            inbox.update_run(settings, run["id"], lambda r: r.update(
                shared_refused=sorted(set(r.get("shared_refused") or []) | {rel})))
        return json.dumps({"behavior": "deny", "message": "사용자가 이 공통 부품의 수정을 허락하지 않았다 — 다른 화면들이 쓰는 부품이다. "
                                                          "고치지 말고 새 화면 쪽에서 해결한다 (다시 신고하거나 묻지 않는다)."},
                          ensure_ascii=False)
    if asked_user:
        # 사용자가 카드에서 거절했다 — 다시 묻거나 같은 일을 다른 표기로 다시 시도하지 않는다 (물어도 허락이 생기지 않는다)
        return json.dumps({"behavior": "deny", "message": f"사용자가 거절함 ({inbox.KINDS.get(kind, kind)}). 같은 일을 다시 시도하거나 "
                                                          "다시 묻지 말고, 다른 방법으로 하거나 하지 못한 것을 인수인계에 적는다."},
                          ensure_ascii=False)
    return json.dumps({"behavior": "deny", "message": f"이 실행의 권한 규칙이 허용하지 않음 ({inbox.KINDS.get(kind, kind)}). 다른 방법을 "
                                                      "찾는다 — 꼭 필요하면 사용자가 직접 하도록 인수인계에 적는다 (물어도 규칙은 바뀌지 않는다)."},
                      ensure_ascii=False)


def late_key(tool_name: str, inp: dict, root: Path) -> str:
    """늦게 온 답을 다시 적용할 때 견주는 모양 — 같은 명령의 다른 표기를 하나로 (도구 + 명령·경로)."""
    if tool_name in ("Bash", "PowerShell"):
        return f"shell:{inbox.command_key(str(inp.get('command') or ''), root)}"
    return f"{tool_name}:{inp.get('file_path') or inp.get('url') or inp.get('command') or json.dumps(inp, sort_keys=True)[:300]}"


def _late_answer(run: dict, tool_name: str, inp: dict, root: Path) -> str | None:
    key = late_key(tool_name, inp, root)
    if key in (run.get("late_refused") or []):
        return "refuse"
    if key in (run.get("late_allowed") or []):
        return "allow"
    return None


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
                             r"|(?<![<>\d=\-])>{1,2}(?![=>])\s*['\"]?[\w.\\/:-]+\.\w+|\btee\s+(?:-a\s+)?['\"]?[\w.\\/:-]+\.\w+"
                             # 제자리 고치기 — sed -i · perl -i (파일을 바꾸는 셸 명령이다)
                             r"|\bsed\s+(?:-[A-Za-z]*\s+)*-[A-Za-z]*i|\bsed\b[^|;&\n]*\s--in-place\b|\bperl\s+(?:-[A-Za-z]*\s+)*-[A-Za-z]*i", re.I)
# 인라인 파이썬(python - <<'EOF' · python -c)이 파일을 쓰는 꼴 — 따옴표를 지우기 전의 원래 글에서 본다
_PY_INLINE_WRITE = re.compile(r"\b(?:python3?|py)(?:\.exe)?\s+(?:-\s*<<|-c\b)[\s\S]*?(?:open\([^)]*['\"][wa]b?\+?['\"]|"
                              r"\.write_(?:text|bytes)\(|json\.dump\(|shutil\.(?:copy|move))", re.I)
_QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\`]|\\.|`.)*\"")


class _ShellWrite:
    """셸로 파일을 쓰는 명령인가 — 따옴표 안의 글(rg "=> res.data" 같은 검색어)은 빼고 본다."""

    @staticmethod
    def search(cmd: str):
        return _SHELL_WRITE_RX.search(_QUOTED.sub("''", str(cmd))) or _PY_INLINE_WRITE.search(str(cmd))


_SHELL_WRITE = _ShellWrite()
# 파이썬 실행기 옵션(-X utf8 · -W ignore · -B -u 등) 뒤의 -c
_PY_PROBE = re.compile(r"(?:^|[\s;&|(\\/])(?:python3?|py)(?:\.exe)?\"?\s+(?:-[XW]\s*\S+\s+|-[BuIEsSOq]+\s+)*-c\s", re.I)


def _native_redirect(tool_name: str, inp: dict, decision: str) -> str | None:
    """에이전트가 자기 권한 모드로 돌 때도 돌려보내는 것 — 셸로 파일을 쓰는 명령(cat > 파일 <<EOF, 인라인 파이썬의 파일 쓰기).
    편집 도구로 쓰면 묻지 않고 바로 되고 편집 직전 확인(순서·공통 부품)도 거치는데, 셸로 쓰면 쓸 때마다 카드가 뜨고 그 확인을
    돌아간다 (실험: Windows 에서 Bash 로 바뀌자 소스·README·합격 케이스를 셸로 써 카드가 다섯 장)."""
    if tool_name not in ("Bash", "PowerShell") or decision != "ask":
        return None
    cmd = str(inp.get("command") or "")
    if inbox.DELETE_CMD.search(cmd):
        return None  # 지우기는 규칙대로 묻는다 (wb_remove 안내는 툴킷 분류 모드에서)
    return REDIRECT_WRITE if _SHELL_WRITE.search(cmd) else None


def _redirect(run: dict, tool_name: str, inp: dict, kind: str, decision: str) -> str | None:
    """물어야 하는 셸 명령 중 툴킷 도구가 맡는 일(테스트·빌드·삭제)이면 카드 대신 그 도구로 돌려보낸다 — 같은 일을 표기만 바꿔
    다시 묻는 일이 없게. 규칙상 바로 허용되는 명령은 그대로 돌고, 도구가 그 일을 못 하는 폴더면 원래대로 묻는다."""
    from beom_workbench.core import sandbox, tasks
    if tool_name not in ("Bash", "PowerShell"):
        return None
    cmd = str(inp.get("command") or "")
    from beom_workbench.core import execenv
    asks = decision == "ask" or (decision == "allow" and tool_name == "PowerShell" and sandbox.wraps_powershell(run)
                                 and inbox.decide(run, "unsandboxed") == "ask"
                                 and not {"outside", "outside:node"} & set(run.get("approved_cmds") or [])
                                 and execenv.outside_reason(cmd, execenv.command_env(Path(run["target_root"]))) is not None)
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
    """프로젝트 작업을 툴킷이 돌린다 — task: "test"(테스트) · "typecheck"(타입 검사: tsc --noEmit·cargo check 등) · "build" · "lint" ·
    "format"(프로젝트의 포매터 설정이 있을 때 형식 검사: prettier --check·ruff format --check·cargo fmt --check). 린트·형식 검사는
    paths 를 비우면 이번 실행에서 바꾼 파일만 본다.
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


_READ_GIT = re.compile(r"\s*git\s+(?:status|diff|log|show|branch\s+--show-current|rev-parse|ls-files|blame)\b(?![^|;&]*[|;&>])", re.I)


@mcp.tool()
def wb_run(command: str, timeout: int = 600) -> dict:
    """격리 밖에서 명령 하나를 돌린다 — 격리(샌드박스) 안에서 '접근 거부'·인터넷 막힘으로 안 되는 명령을 돌릴 때 (Codex, 그리고
    셸 명령이 격리되는 Claude).
    의존성 설치(npm install·pip install 등)는 wb_deps, 테스트·빌드는 wb_task 로 한다 (여기로 보내면 그쪽으로 돌려보낸다). 권한 규칙을 그대로 거친다: 명령의 종류(빌드·명령 실행 등)와 '격리 없이 실행'이 모두 허용이면
    바로 돌고, 아니면 사용자에게 카드로 묻는다 (시스템 변경·게시·브랜치 바꾸기는 거절). 대상 폴더에서 PowerShell(Windows)·sh 로
    돌리고 종료 코드와 끝 출력을 돌려준다. 시간이 넘으면 끝낸다 (기본 600초, 최대 1800초).
    결과물(README·스크립트)에는 이 도구 이야기를 쓰지 않는다 — 사용자는 평소처럼 npm install 을 친다."""
    import subprocess
    import sys
    from beom_workbench.core import port

    run = _run()
    if run is None:
        return {"ok": False, "error": "자동 실행 밖"}
    from beom_workbench.core import agents as _agents, sandbox as _sbx
    if run.get("agent", "claude") != "codex" and not _sbx.active(run):
        return {"ok": False, "error": "이 실행은 셸 명령을 격리하지 않는다 — 셸 도구로 실행한다"}
    cmd = (command or "").strip()
    if not cmd:
        return {"ok": False, "error": "command 가 비어 있음"}
    root = Path(run["target_root"])
    if inbox._read_only_shell(cmd, root) or inbox._inspect_only(cmd) or _READ_GIT.match(cmd):
        # 읽기·확인은 격리 안에서도 된다 — 격리 밖으로 보내면 명령마다 허락 카드가 뜬다 (실험: Codex 가 rg·git status 를 모두 이리로)
        inbox.log(settings, run["id"], "permission", tool="mcp:wb_run", permission_kind="edit", decision="redirect", what=mask_command(cmd[:160]))
        return {"ok": False, "error": "읽기·확인 명령(rg·Get-Content·git status/diff/log 등)은 자기 셸 도구로 돌린다 — wb_run 은 격리 때문에 "
                                      "막히는 명령만 (사용자 허락이 필요하다)"}
    if run.get("agent") == "codex" and _agents.get("codex").app_server():
        return {"ok": False, "error": "격리 때문에 막힌 명령은 그 명령을 권한 상승(escalated permissions)으로 다시 요청한다 — 툴킷이 권한 "
                                      "규칙대로 허락하거나 사용자에게 묻는다. 테스트·빌드는 wb_task, 설치는 wb_deps."}
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
            inbox.allow_always(settings, run["id"], "unsandboxed")
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
    from beom_workbench.core import execenv
    env = execenv.command_env(root)  # 프로젝트가 정한 JDK·Node 와 준비한 의존성 — 툴킷의 다른 실행 자리와 같게
    try:
        cp = port.run_tree(argv, cwd=str(root), timeout=max(10, min(int(timeout or 600), 1800)), env=env)
    except subprocess.TimeoutExpired as e:
        tail = ((e.output or b"") + (e.stderr or b"")).decode("utf-8", "replace")[-3000:]
        return {"ok": False, "error": f"{timeout}초 안에 끝나지 않아 멈췄다", "output_tail": tail}
    out = (cp.stdout + cp.stderr).decode("utf-8", "replace")
    inbox.log(settings, run["id"], "unsandboxed_run", command=mask_command(cmd[:300]), exit_code=cp.returncode)
    return {"ok": cp.returncode == 0, "exit_code": cp.returncode, "output_tail": out[-4000:]}


@mcp.tool()
@_record_refusal
def wb_person_ask(question: str, choices: list[str] | None = None, questions: list[dict] | None = None) -> dict:
    """사용자에게 묻고 답을 기다린다(앱의 질문 카드). 요구사항이 애매하거나 결정이 필요할 때. 답이 없으면 timeout.
    처음 보는 사람이 읽고 바로 답할 수 있게 쓴다 — 질문마다: 무엇을 정하는지(사용자가 아는 말로), 지금 무엇을 근거로 묻는지(본 문서·코드·
    조사), 선택지마다 실제로 무엇을 하게 되는지 한 줄, 추천과 그 까닭. 전문 용어·서비스 이름·줄임말은 처음 나올 때 괄호로 풀이한다
    (예: 'GTIN(상품 바코드 번호)'). 지금 바로 실행하는 일인지, 문서·계획에 쓸 방향을 정하는 것인지도 밝힌다.
    고를 수 있는 질문은 questions 로 준다 — 카드에 단추(하나만: 동그라미, 여럿: 네모)로 나오고 추천이 미리 골라져 있으며, 사용자는
    고르거나 '직접 입력'에 적어 한 번에 보낸다. 항목: {"question": 질문 한두 줄, "options": [짧은 이름 2~6개], "recommended": 추천
    (이름 하나, multi 면 목록), "multi": 여럿 고르기면 true, "why": 추천 까닭 한 줄(선택)}. 이때 question 은 왜 묻는지 한 줄이다.
    필요한 질문은 빠짐없이 한 번에 준다 — 4개가 넘으면 툴킷이 4개씩 카드를 나눠 차례로 묻고, 끝에 고른 답을 모아 보여 주며
    최종 확인을 받는다. 한 번에 12개가 넘으면 앞 12개를 먼저 묻고 not_asked 로 알린다 — 남은 것은 그 답을 반영해 다시 묻는다.
    문서·코드·검색으로 알 수 있는 것은 묻지 않는다. 앞 답에 따라 달라지는 질문만 그 답을 받은 뒤 다시 묻는다.
    선택지로 나눌 수 없는 질문(이름·값을 적어야 하는 것)만 question 글로 묻는다."""
    run = _run()
    if run is None:
        return {"ok": False, "error": "자동 실행 밖에서는 사용자에게 직접 묻는다"}
    qs, bad = _ask_items(questions)
    rest = int(bad.split(":")[1]) if bad.startswith("rest:") else 0
    if bad and not rest:
        return {"ok": False, "error": bad}
    from beom_workbench.core import runner as runner_core
    ui = runner_core.ui_task(run) if (run.get("plan") or {}).get("steps") else runner_core.screens_at_start(run, at_least=1)
    fields = [("질문", question), *[(f"선택지 {i + 1}", c) for i, c in enumerate(choices or [])]]
    for n, q in enumerate(qs, 1):
        fields += [(f"질문 {n}", q["question"]), *[(f"질문 {n} 선택지", o) for o in q["options"]],
                   *([(f"질문 {n} 까닭", q["why"])] if q.get("why") else [])]
    jargon = _card_jargon(run, fields, paths=ui, idents=ui)
    if jargon:  # 묻기 전에 — 사용자가 못 알아듣는 질문은 답도 엉뚱해진다
        return {"ok": False, "error": "질문에 사용자가 모르는 내부 표시가 있다: " + "; ".join(jargon[:4]) + " — 요구사항 번호 대신 그 내용을, "
                "파일·코드 이름 대신 화면에서 보이는 것을 말로 쓰고 다시 묻는다"}
    if not qs:
        ans = inbox.ask(settings, run["id"], "question", {"question": question, "choices": choices or []})
        return {"ok": not ans.get("timeout"), "answer": ans.get("text") or ans.get("choice"), "timeout": bool(ans.get("timeout"))}
    # 4개씩 나눠 차례로 — 한 카드에 너무 많으면 대충 답한다. 여럿으로 나눴으면 끝에 모아 최종 확인을 받는다
    parts = [qs[i:i + ASK_PER_CARD] for i in range(0, len(qs), ASK_PER_CARD)]
    texts, answers = [], []
    for k, part in enumerate(parts):
        start = k * ASK_PER_CARD
        ans = inbox.ask(settings, run["id"], "question", {
            "question": question, "choices": [], "questions": part, "start": start, "part": [k + 1, len(parts)],
            "summary": "\n".join([question.strip() + (f" ({k + 1}/{len(parts)})" if len(parts) > 1 else ""),
                                  *[f"{start + n}. {q['question']}" for n, q in enumerate(part, 1)]]).strip()})
        if ans.get("timeout"):
            return {"ok": False, "answer": "\n".join(texts) or None, "timeout": True}
        texts.append(ans.get("text") or ans.get("choice") or "")
        answers += ans.get("answers") or []
    text = "\n".join(t for t in texts if t)
    if len(parts) > 1:
        fin = inbox.ask(settings, run["id"], "question", {
            "question": "지금까지 고른 답입니다 — 이대로 정할까요? 바꿀 것이 있으면 적어 주세요.\n\n" + text, "choices": ["이대로 정하기"],
            "final": True, "summary": "고른 답 최종 확인"})
        if fin.get("timeout"):
            return {"ok": False, "answer": text, "timeout": True}
        if fin.get("text"):  # 고칠 것을 적었다 — 앞의 답보다 이 말이 우선한다
            text += "\n[최종 확인에서 사용자가 고친 것 — 앞의 답보다 우선] " + fin["text"]
    out = {"ok": True, "answer": text, "answers": answers, "timeout": False}
    if rest:  # 한 번에 너무 많았다 — 남은 질문은 이 답을 반영해 다시 묻는다 (이미 정해진 것·문서와 코드로 알 수 있는 것은 빼고)
        out["not_asked"] = (f"질문이 {ASK_MAX + rest}개여서 앞 {ASK_MAX}개만 물었다 — 남은 {rest}개 중 이 답으로 정해졌거나 문서·코드로 "
                            "알 수 있는 것은 빼고, 남은 것만 다시 wb_person_ask 로 묻는다")
    return out


def _handoff_choices(choices: list[dict] | None) -> tuple[list[dict], list[str]]:
    """인계의 '고를 것'을 카드에 그릴 모양으로 — (다듬은 것, 고칠 점). 설명 없이 이름만 늘어놓으면 사용자가 무엇을 골라야 할지 몰랐다."""
    picks, bad = [], []
    for n, c in enumerate((choices or [])[:8], 1):
        if not isinstance(c, dict):
            continue
        q = str(c.get("question") or "").strip()[:160]
        opts = []
        for o in (c.get("options") or [])[:4]:
            name, desc = (str(o.get("name") or "").strip(), str(o.get("desc") or "").strip()) if isinstance(o, dict) else (str(o).strip(), "")
            if name:
                opts.append({"name": name[:60], "desc": desc[:200]})
        if not q or len(opts) < 2:
            continue
        rec = str(c.get("recommended") or "").strip()
        tag = str(c.get("id") or f"{n}번").strip()[:8]
        missing = [x for x, ok in (("context", str(c.get("context") or "").strip()), ("why", str(c.get("why") or "").strip())) if not ok]
        if any(not o["desc"] for o in opts):
            missing.append("options[].desc")
        if rec and rec not in [o["name"] for o in opts]:
            bad.append(f"고를 것 {tag}: recommended 는 options 의 name 중 하나여야 한다")
        if missing:
            bad.append(f"고를 것 {tag}: {', '.join(missing)} 가 없다 — 무엇을 정하는지·고르면 어떻게 되는지·추천 까닭이 카드에 보여야 사용자가 고른다")
        picks.append({"id": tag, "question": q, "context": str(c.get("context") or "").strip()[:300], "options": opts,
                      "recommended": rec if rec in [o["name"] for o in opts] else "", "why": str(c.get("why") or "").strip()[:240]})
    return picks, bad


def _ask_items(questions: list[dict] | None) -> tuple[list[dict], str]:
    """questions 를 카드에 그릴 수 있게 다듬는다 — 모양이 틀리면 (빈 목록, 고칠 점)."""
    out = []
    for n, q in enumerate(questions or [], 1):
        if not isinstance(q, dict) or not str(q.get("question") or "").strip():
            return [], f"questions[{n}] 에 question 이 없다"
        opts = list(dict.fromkeys(str(o).strip() for o in (q.get("options") or []) if str(o).strip()))
        if not 2 <= len(opts) <= 6:
            return [], f"questions[{n}] 의 options 는 2~6개다 (지금 {len(opts)}개) — 선택지로 나눌 수 없으면 question 글로 묻는다"
        multi = bool(q.get("multi"))
        rec = q.get("recommended")
        rec = [str(r).strip() for r in (rec if isinstance(rec, list) else [rec] if rec else [])]
        if any(r not in opts for r in rec):
            return [], f"questions[{n}] 의 recommended 는 options 중 하나여야 한다 ({', '.join(r for r in rec if r not in opts)})"
        if not multi and len(rec) > 1:
            return [], f"questions[{n}] 은 하나만 고르는 질문인데 추천이 여럿이다 — multi: true 로 하거나 추천을 하나로"
        out.append({"question": str(q["question"]).strip(), "options": opts, "recommended": rec, "multi": multi,
                    **({"why": str(q["why"]).strip()} if str(q.get("why") or "").strip() else {})})
    if len(out) > ASK_MAX:  # 막지 않는다 — 앞 12개를 먼저 묻고, 나머지는 그 답을 받은 뒤 다시 묻게 알린다 (물을 것은 다 묻는다)
        return out[:ASK_MAX], f"rest:{len(out) - ASK_MAX}"
    return out, ""


ASK_PER_CARD = 4   # 한 카드에 — 넘으면 나눠 차례로 묻는다
ASK_MAX = 12       # 한 번에 (카드 세 장)


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
    return [x for s in steps for x in s.get("files") or [] if str(x).lower().endswith(exts)]


def _path_part(ref: str) -> str:
    """'경로:줄' · '경로:줄-줄' · '경로#L줄' 에서 경로만 — D:\\repo\\a.md 같은 드라이브 문자는 그대로 둔다."""
    import re as _re
    return _re.sub(r"(?::\d+(?:[-:]\d+)?|#L\d+(?:-L?\d+)?)(?:\s.*)?$", "", str(ref).strip())


DOC_SUFFIXES = (".md", ".mdx", ".txt", ".pdf", ".docx", ".adoc", ".rst")


def _follows(run: dict | None, steps: list[dict], follows: list[dict] | None, scope: str | None = None) -> tuple[list[dict] | None, list[str], dict | None]:
    """계획의 '기존 규칙 따름' — 근거(파일:줄)가 실제 기존 코드인지 확인한다. 기존 화면이 여럿 있는 프로젝트에서 화면 파일을
    만들·고치는 계획인데 근거가 없으면 (None, [이유…]) — 계획을 받지 않는다. 틀린 근거는 빼고 알린다."""
    from beom_workbench.core import conventions, runner as runner_core
    if run is None:
        return [], [], None
    root = Path(run["target_root"])
    rules, errors = [], []
    for f in follows or []:
        if not isinstance(f, dict) or not str(f.get("rule", "")).strip():
            continue
        ok, bad = conventions.check_evidence(root, [str(x) for x in f.get("evidence") or []][:6])
        errors += [f"근거 확인 실패: {b}" for b in bad]
        if ok:
            # code: 규칙이 가리키는 코드 쪽 이름(부품·이벤트·옵션) — 카드의 규칙 글은 사용자의 말로 두고 이름은 여기에 (실험: 규칙 글에
            # onRowDblClick·pageSize 를 그대로 써 계획이 세 번 중 두 번 돌아왔다)
            raw = f.get("code")
            code = [str(x).strip()[:60] for x in (raw if isinstance(raw, list) else [raw] if raw else []) if str(x).strip()][:6]
            rules.append({"rule": str(f["rule"]).strip()[:200], "evidence": ok, **({"code": code} if code else {})})
    ui = _screen_files(steps)
    # 디자인을 바꾸는 작업(느낌만·다시 디자인)은 기존 화면의 모습을 따르는 일이 아니다 — 따른 근거를 묻지 않는다 (실험: 느낌만 바꾸는 실행이
    # 매번 이것으로 첫 계획을 돌려받았다. 배치·기능을 지켰는지는 디자인 점검과 합격 케이스가 본다)
    if ui and runner_core.screens_at_start(run) and scope not in ("restyle", "redesign", "missing"):
        problems, conv = [], None
        # 기존 파일을 직접 읽어 근거를 대도, 툴킷이 뽑은 목록(문구 키·클래스·상수·데이터 함수·서버 쿼리)을 보지 않으면 이미 있는 것을
        # 다시 만들었다 (실험: Claude 실행이 이 도구를 한 번도 부르지 않고 같은 값의 문구 키·값 목록을 새로 만들었다). 돌려보내기만
        # 하면 도구를 부르고 다시 내느라 두 번을 더 쓴다 (실험: 화면 실행 8개 중 5개) — 툴킷이 계획한 화면 파일로 대신 뽑아 돌려보내는
        # 답에 싣는다. 에이전트는 그것을 보고 steps 를 고쳐 한 번에 다시 낸다
        # 그래서 툴킷이 계획한 화면 파일로 대신 뽑는다. 계획에 이미 기존 화면에서 확인한 규칙(근거 파일 2개 이상)이 있으면 계획은 받고
        # 뽑은 것을 답에 실어 구현 때 쓰게 한다 — 도구를 안 불렀다는 까닭만으로 돌려보내면 다시 내느라 한 번을 더 썼다
        if not any(e.get("kind") == "ui_conventions" for e in inbox.events(settings, run["id"])[0]):
            conv = conventions.scan(root, near=ui[:10])
            inbox.log(settings, run["id"], "ui_conventions", near=ui[:10], by="plan_set")
        # 근거는 다른 화면들의 코드 — 문서(정책 조항은 rules_checked·근거 문서로 따로 본다)·.beom(시안·메모)·이번에 만든 파일은 세지 않는다
        before = _files_at_start(run)
        files = {f for f in (_rel(_path_part(e)) for r in rules for e in r["evidence"])
                 if not f.startswith(".beom/") and not f.lower().endswith(DOC_SUFFIXES) and (before is None or f in before)}
        need = 2 if runner_core.screens_at_start(run, at_least=2) else 1  # 기존 화면이 하나뿐이면 그 하나가 근거다
        if len(files) < need:
            problems.append("기존 화면이 있는 프로젝트에서 화면(" + ", ".join(ui[:3]) + ")을 만들거나 고치는 계획인데, 다른 화면들의 규칙을 "
                            "확인한 근거가 없음 — 비슷한 기존 화면과 공통 부품의 쓰임을 보고 follows=[{rule, evidence:[\"파일:줄\", …], code?}] "
                            f"로 따를 규칙을 남긴다 (작업 전부터 있던 서로 다른 코드 파일 {need}개 이상 — 문서·.beom 파일은 세지 않는다)"
                            + (". 툴킷이 계획한 화면 파일로 뽑은 기존 규칙(공통 부품·문구 키·클래스·상수·데이터 함수)을 이 답의 conventions 에 실었다"
                               if conv is not None else ""))
        if problems:
            return None, problems + errors, conv
        return rules[:12], errors, conv
    return rules[:12], errors, None


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
    unread = _unread_docs(run, [x for x in said if norm(x).exists()])
    if not missing and doc_ok and not unread:
        return [x for x in said if norm(x).exists()], None
    if not missing and doc_ok:
        return [], ("읽었다고 적은 규칙 문서를 연 기록이 없다: " + ", ".join(unread[:6]) + " — 목록에 적는 것이 아니라 읽는 것이 일이다. "
                    "파일을 열어 읽고(읽기 도구나 셸로) 다시 낸다. 툴킷은 이번 실행에서 그 파일을 연 기록을 본다")
    parts = []
    if missing:
        parts.append("에이전트 지시·규칙 허브 " + ", ".join(missing))
    if not doc_ok:
        parts.append("정책·가이드 문서(" + ", ".join(some[:5]) + ") 중 이번 작업에 닿는 것")
    return [], ("대상 저장소의 규칙 문서를 읽었다고 밝히지 않음: " + " · ".join(parts) +
                " — 읽고 rules_checked=[\"문서 경로\", …] 로 남기고, 따를 조항은 follows 근거(문서:줄)로 든다")


AUTO_LOADED = {"agents.md", "claude.md", "gemini.md"}  # 에이전트 프로그램이 스스로 읽어 들이는 지침 파일 — 읽은 것으로 본다


def _unread_docs(run: dict, docs: list[str]) -> list[str]:
    """적은 문서 중 이번 실행에서 에이전트가 연 기록이 없는 것 — 에이전트의 읽기·검색·셸 명령에 그 파일 이름이 나오는지 (툴킷 도구 호출은
    세지 않는다 — 계획 제출 글에도 그 이름이 있다). 실험: '읽은 규칙 문서'를 목록으로만 받으니, 열어 본 적 없이 적어도 지나갔다."""
    if not docs:
        return []
    seen = []
    for e in inbox.events(settings, run["id"])[0]:
        if e.get("kind") != "agent":
            continue
        for it in e.get("items") or []:
            tool = str(it.get("tool") or "")
            if tool and "wb_" not in tool:
                seen.append(str(it.get("input") or "").replace("\\\\", "/").replace("\\", "/"))
    blob = chr(10).join(seen)
    return [d for d in docs if Path(_path_part(d)).name.lower() not in AUTO_LOADED and Path(_path_part(d)).name not in blob]


def _design_pick(picks: list[dict]) -> dict | None:
    """디자인 방향 결정 — 시안(looks)이 붙은 결정. 주제 글을 읽어 짐작하지 않는다 (시안이 붙은 결정이 곧 디자인 방향이다)."""
    return next((d for d in picks if d.get("looks") or d.get("shots")), None)


from beom_workbench.core.scaffold import LOCKFILES  # noqa: E402 — 잠금 파일 목록은 한 곳


def _undo_code_before_plan(run: dict, prev: dict) -> list[str]:
    """요청사항 실행의 첫 계획 — 그 전에 만들거나 고친 제품 파일을 시작 전 상태로 되돌린다. Claude 는 편집 문지기(hookgate)가 미리
    막지만 Codex 는 편집을 자기 격리 안에서 바로 해서 툴킷이 미리 막을 길이 없다 — 순서는 모델이 아니라 툴킷이 지킨다.
    툴킷 작업 파일(.beom)과 의존성 설치가 만든 잠금 파일은 둔다."""
    if run.get("mode") not in ("request", "reference") or prev.get("steps") or (prev.get("history") or []):
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


# 결과를 바꾸는 구체적인 값 — 최대 글자 수·기간·개수·금액·비율 (0·1 은 '없음/하나'라 보지 않는다)
VALUE_RX = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(자|글자|원|일|개|건|명|회|번|분|초|시간|%|퍼센트|장|주|개월|년|"
                      r"MB|KB|GB|px|단계|줄|자리|점|살|세|km|kg|ms)(?![A-Za-z])")  # 뒤에 조사·어미가 붙어도 (50자다·30일까지)
VALUE_LOOK_EXTS = {".css", ".scss", ".sass", ".less", ".svg"}  # 스타일 파일 — 값의 근거로 세지 않는다 (HTML 의 maxlength 는 근거다)
VALUE_SKIP_DIRS = {".beom", ".git", "node_modules", ".venv", "venv", "dist", "build", "target", "__pycache__", ".next", "coverage"}
VALUE_EXTS = {".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte", ".html", ".htm", ".jsp", ".j2", ".jinja", ".css", ".scss",
              ".json", ".yml", ".yaml", ".toml", ".md", ".txt", ".java", ".kt", ".cs", ".go", ".rb", ".php", ".sql", ".xml", ".properties"}


def _user_rows(run: dict) -> dict[str, str]:
    """사용자가 앱에서 직접 쓴 요구사항 {번호: 글} (출처 기록이 from=user 인 것)."""
    meta = run.get("req_meta") if isinstance(run.get("req_meta"), dict) else {}
    ids = {k for k, m in meta.items() if isinstance(m, dict) and m.get("from") == "user"}
    if not ids or not run.get("project_id") or not run.get("port_id"):
        return {}
    from beom_workbench.core import make, project
    try:
        conn, _ = project.load(settings, run["project_id"])
        return {r["id"]: str(r.get("text") or "") for r in make.requirements(conn, run["port_id"]) if r["id"] in ids}
    except Exception:  # noqa: BLE001 — 원장을 못 읽으면 없는 것으로
        return {}


def _user_words(run: dict) -> str:
    """사용자가 직접 한 말만 — 요청·이어서 보낸 말·질문의 답과 답한 질문(추천대로 고른 안). 기획 문서는 넣지 않는다
    ('사용자 결정' 줄이 자기 문서를 근거로 삼지 않게)."""
    texts = [str(run.get("request") or ""), *[str(x.get("text") or "") for x in run.get("messages") or []]]
    evs = inbox.events(settings, run["id"])[0]
    answered = {e.get("id") for e in evs if e.get("kind") == "answer" and isinstance(e.get("answer"), dict) and not e["answer"].get("timeout")}
    texts += [json.dumps(e.get("answer"), ensure_ascii=False) for e in evs if e.get("kind") == "answer"]
    texts += [json.dumps({k: e.get(k) for k in ("summary", "question", "questions", "choices") if e.get(k)}, ensure_ascii=False)
              for e in evs if e.get("kind") == "ask" and e.get("id") in answered]
    return "\n".join(texts)


def _user_texts(run: dict) -> list[str]:
    """사용자의 말 — 요청·이어서 보낸 말·질문의 답·기획 패키지(사용자가 고른 기획으로 만들 때)."""
    texts = [str(run.get("request") or ""), *[str(x.get("text") or "") for x in run.get("messages") or []]]
    evs = inbox.events(settings, run["id"])[0]
    texts += [json.dumps(e.get("answer"), ensure_ascii=False) for e in evs if e.get("kind") == "answer"]
    # 답한 질문 — '추천대로'라는 답은 질문에 적힌 추천안을 고른 것이다 (답이 없거나 시간이 지난 질문은 아니다)
    answered = {e.get("id") for e in evs if e.get("kind") == "answer" and isinstance(e.get("answer"), dict) and not e["answer"].get("timeout")}
    texts += [str(e.get("summary") or "") for e in evs if e.get("kind") == "ask" and e.get("ask_kind") == "question" and e.get("id") in answered]
    texts += _user_rows(run).values()  # 사용자가 앱에서 직접 쓴 요구사항도 사용자의 말이다 (그 값·이름은 근거가 된다)
    # 같은 폴더의 앞 작업에서 사용자가 '다음에도 이렇게'라고 남긴 말 — 사용자가 직접 쓴 말이다 (실험 149: 그 말을 인용한 요구사항을
    # '사용자의 말에 없다'고 돌려보냈다)
    texts += [ln[2:] for block in run.get("lessons") or [] if "다음에도" in str(block).split("\n", 1)[0]
              for ln in str(block).splitlines() if ln.startswith("- ")]
    brief = Path(run["target_root"]) / ".beom" / "brief"
    if brief.is_dir():
        texts += [f.read_text(encoding="utf-8", errors="replace")[:200_000] for f in brief.rglob("*") if f.suffix.lower() in (".md", ".txt")]
    return texts


# 카드에 보이는 글에 들어간 내부 표시 — 요구사항 번호(R7)·이 프로젝트의 파일 경로·이 프로젝트 코드의 이름(createGrid·user_id·render())
# 실험: 모의 사용자가 'R7, createGrid, js/notice-logic.js 같은 말 때문에 카드를 이해하기 어렵다'며 계획을 돌려보냈다.
# 번호는 steps.requirements 에, 파일은 steps.files 에 따로 적는 칸이 있다 — 카드 글은 화면·동작의 말로.
# 낱말 목록으로 가르지 않는다 — 경로·이름이 이 프로젝트에 실제로 있는지(사실)로 가른다. 'sessionStorage'·'SQLite' 처럼 프로젝트 밖의
# 기술 이름은 사용자(개발 리드)도 쓰는 말이다 (실험: 예외 낱말 목록이 늘 뒤처져 'sessionStorage' 결정·'404' 를 돌려보냈다).
CARD_RID = re.compile(r"(?<![A-Za-z0-9_])R\d{1,3}(?![A-Za-z0-9_])")
CARD_PATH = re.compile(r"(?<![\w/.@-])(?:[\w.-]+/)*[\w-]+\.(?:py|js|mjs|cjs|ts|tsx|jsx|vue|svelte|html?|jsp|j2|jinja2?|css|scss|less|json|"
                       r"ya?ml|toml|java|kt|cs|go|rb|php|sql|xml|properties|md|sh|ps1|gradle)(?![\w])", re.I)
CARD_IDENT = re.compile(r"(?<![\w./-])(?:[a-z][a-z0-9]*[A-Z][A-Za-z0-9]*|[a-z][a-z0-9]*(?:_[a-z0-9]+)+|[A-Za-z_]\w*\(\))(?![\w/-])")
CARD_OPEN = {"index.html", "readme.md"}  # 사용자가 직접 여는 파일 — 사용자의 말이다
_NAMES_CACHE: dict[str, tuple[float, set, set]] = {}


def _project_names(run: dict) -> tuple[set[str], set[str]]:
    """대상 폴더의 파일 경로(소문자 posix)와 코드·문서에 나오는 이름 — 카드 글의 경로·이름이 이 프로젝트의 것인지 가른다.
    한 계획을 보는 동안 여러 번 부르므로 잠깐 담아 둔다."""
    import time
    root = Path(run["target_root"])
    key = str(root.resolve())
    hit = _NAMES_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < 20:
        return hit[1], hit[2]
    paths: set[str] = set()
    names: set[str] = set()
    seen = 0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in VALUE_SKIP_DIRS]
        for name in files:
            f = Path(dirpath) / name
            try:
                paths.add(f.relative_to(root).as_posix().lower())
            except ValueError:
                continue
            if f.suffix.lower() in VALUE_EXTS and seen < 3000:
                seen += 1
                try:
                    names.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", f.read_text(encoding="utf-8", errors="replace")[:300_000]))
                except OSError:
                    pass
    _NAMES_CACHE.clear()
    _NAMES_CACHE[key] = (time.monotonic(), paths, names)
    return paths, names


def _card_jargon(run: dict, fields: list[tuple[str, str]], words: list[str] | None = None, paths: bool = True,
                 idents: bool = True, planned: list[str] | tuple = (), new_code: bool = False) -> list[str]:
    """카드 글(fields: [(어디, 글)])에서 사용자의 말에 없는 내부 표시를 찾는다 — 요구사항 번호, 이 프로젝트에 있는(또는 계획한) 파일
    경로, 이 프로젝트 코드에 나오는 이름. 사용자가 먼저 쓴 말(요청의 'js/data.js'·'parseWon')은 된다.
    new_code: 코드가 아직 없는 새 프로젝트 — 견줄 코드가 없으니 코드 모양의 이름(validateOrder·user_id)은 모두 에이전트가 지은 이름이다
    (실험: 견줄 코드가 없어 새 프로젝트 카드의 'validateOrder' 를 놓쳤다). 기술 이름을 쓰라고 한 칸(결정의 why)은 부르는 쪽이 뺀다."""
    pool = chr(10).join(words if words is not None else _user_texts(run))
    proj_paths, proj_names = _project_names(run) if (paths or idents) else (set(), set())
    if new_code and idents:
        proj_names = proj_names | _declared_names(run, planned)
    known = proj_paths | {_rel(x).lower() for x in planned}

    def is_path(x: str) -> bool:
        lx = _rel(x).lower()
        return lx not in CARD_OPEN and any(p == lx or p.endswith("/" + lx) for p in known)

    out = []
    for where, text in fields:
        t = str(text or "")
        hits = [m.group(0) for m in CARD_RID.finditer(t)]
        if paths:
            hits += [m.group(0) for m in CARD_PATH.finditer(t) if is_path(m.group(0))]
        if idents:
            hits += [m.group(0) for m in CARD_IDENT.finditer(t) if m.group(0).rstrip("()") in proj_names]
        hits = [x for x in dict.fromkeys(hits) if x not in pool and x.rstrip("()") not in pool]
        if hits:
            out.append(f"{where}: " + ", ".join(hits[:4]))
    return out


def _declared_names(run: dict, planned) -> set[str]:
    """새 프로젝트에서 에이전트가 지은 코드 이름 — 합격 케이스가 부르는 함수(파일:함수)와 계획한 파일 이름. 'localStorage'·'iOS' 같은
    바깥 기술 이름은 여기에 없다 (실험: 코드 모양의 낱말을 모두 막으니 'localStorage' 요구사항이 돌아왔다)."""
    names: set[str] = {Path(str(f)).stem for f in planned or []}
    for c in _golden_cases(run) if run.get("port_id") else []:
        call = c.get("call")
        for x in (call.values() if isinstance(call, dict) else [call]):
            if isinstance(x, str) and ":" in x:
                names.add(x.rsplit(":", 1)[1].strip())
    return {n for n in names if n}


_REC_TAG = re.compile(r"^\s*[\[(（［【]\s*(?:추천(?:안)?|recommended)\s*[\])）］】]\s*", re.I)


def _golden_cases(run: dict) -> list[dict]:
    """저장한 합격 케이스 (못 읽으면 빈 목록)."""
    from beom_workbench.core import project
    try:
        g = project.workspace(settings, run["project_id"]) / "ports" / run["port_id"] / "golden.json"
        cases = json.loads(g.read_text(encoding="utf-8")).get("cases") or [] if g.is_file() else []
    except Exception:  # noqa: BLE001
        return []
    return [c for c in cases if isinstance(c, dict)]


VALUE_DECIDE = "사용자가 정할 값이면 계획의 decisions 에 추천안(choice)과 대안(options)으로 내고 그 요구사항을 from=decision 으로 잇는다"
VALUE_OTHER = ("지금 있는 동작의 값이면 from=existing 과 그 값이 적힌 파일(file)로 잇는다. 데이터 개수·예시 입력처럼 동작을 정하는 규칙이 "
               "아닌 값은 요구사항 문장에서 빼고 합격 케이스에 둔다")


def _unsourced_values(run: dict, picks: list[dict], reqs: list[dict] | None = None, meta: dict | None = None) -> list[str]:
    """요구사항 문장의 구체적인 값(최대 50자·30일·3장 …)이 어디서 왔나 — 요청·이어서 보낸 말·질문의 답·첨부·계획의 결정(사용자가 카드에서
    고른다)·기존 코드에서 찾지 못한 값. 실험: '제목 최대 50자'·'100자'를 요청에 없이 확정처럼 적었고 사람(모의 사용자)만 잡았다.
    다른 명세 도구들도 근거 없는 값을 '확인 필요'로 표시해 묻는다 — 여기서는 툴킷이 찾아보고 없으면 결정으로 내게 한다."""
    from beom_workbench.core import make, project
    if reqs is None:
        try:
            conn, _ = project.load(settings, run["project_id"])
            reqs = make.requirements(conn, run["port_id"])
        except Exception:  # noqa: BLE001 — 원장을 못 읽으면 막지 않는다
            return []
    meta = meta if meta is not None else _req_meta(run)
    wanted = []
    for r in reqs:
        if (meta.get(str(r.get("id"))) or {}).get("from") == "user":
            continue  # 사용자가 직접 쓴 요구사항 — 그 값은 사용자가 정한 것이다
        for m in VALUE_RX.finditer(str(r.get("text") or "")):
            num = m.group(1)
            if num.replace(",", "") not in ("0", "1"):
                wanted.append((r.get("id"), m.group(0), num))
    if not wanted:
        return []
    from beom_workbench.core import screens as screens_core
    # 툴킷 자신이 정한 값(휴대폰·PC 점검 폭)도 근거다 — 툴킷이 그 폭으로 재라고 했는데 그 값을 '근거 없음'으로 돌려보냈다 (실험 129)
    own = f"{screens_core.MOBILE[0]}px {screens_core.DESKTOP[0]}px"
    texts = _user_texts(run) + [json.dumps(picks, ensure_ascii=False), own]
    root = Path(run["target_root"])

    def found(num: str, pool: str) -> bool:
        forms = {num, num.replace(",", "")}
        return any(re.search(rf"(?<![\d.]){re.escape(f)}(?![\d])", pool) for f in forms)

    def in_code(num: str, pool: str) -> bool:
        # 기존 코드 파일에 그 수가 그대로 있나 — 어떤 이름 옆의 값인지 낱말 목록으로 가르지 않는다. 화면 모양 파일(스타일·마크업)의
        # 수는 세지 않는다 ('width:100%' 의 100 이 '제목 최대 100자'의 근거가 되었다 — 실험)
        return found(num, pool)

    head = chr(10).join(texts)
    left = [w for w in wanted if not found(w[2], head)]
    # 출처가 '지금 있는 동작'(existing + file)이면 그 파일에 그 값이 있나 — 에이전트가 가리킨 곳을 사실로 본다

    def in_origin(rid, num: str) -> bool:
        o = meta.get(str(rid)) or {}
        f = _origin_file(o, root) if o.get("from") == "existing" else ""
        try:
            return bool(f) and found(num, (root / f).read_text(encoding="utf-8", errors="replace")[:400_000])
        except OSError:
            return False

    left = [w for w in left if not in_origin(w[0], w[2])]
    if not left:
        return []
    before = _files_at_start(run)  # 기존 코드 — 실행 전부터 있던 파일만 (새로 쓴 코드의 값은 근거가 아니다)
    seen = 0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in VALUE_SKIP_DIRS]
        for name in files:
            f = Path(dirpath) / name
            if f.suffix.lower() not in VALUE_EXTS or f.suffix.lower() in VALUE_LOOK_EXTS or seen > 3000:
                continue
            seen += 1
            if before is not None and f.relative_to(root).as_posix() not in before:
                continue
            try:
                pool = f.read_text(encoding="utf-8", errors="replace")[:400_000]
            except OSError:
                continue
            left = [w for w in left if not in_code(w[2], pool)]
            if not left:
                return []
    return list(dict.fromkeys(f"{rid} '{txt}'" for rid, txt, _ in left))


# 요구사항의 출처 — 요구사항이 요청에서 왔는지를 낱말(검색·정렬·관리자만 …)로 짐작하지 않는다. 에이전트가 요구사항마다 출처를 적고,
# 툴킷은 적힌 것이 사실인지만 본다: 인용이 사용자의 말에 그대로 있나, 기대는 결정이 계획에 있고 그 안이 고른 안인가, 파일이 원래
# 있었나, 받쳐 주는 요구사항이 있나. 다른 명세 도구들이 요구사항마다 근거를 잇는(traceability) 것과 같다. 실험: 낱말 목록이
# '관리자만'·'받을 회원을 골라 보낸다'·'기능은 그대로'를 잘못 읽어 계획을 돌려보냈고, 목록에 없는 말로 바꿔 쓰면 그대로 지나갔다.
ORIGINS = ("request", "decision", "existing", "needed")
ORIGIN_HOW = ("from 은 request(요청·이어서 보낸 말·질문의 답에 있는 말을 quote 에 그대로) · decision(계획에서 사용자가 고를 결정의 이름을 "
              "decision 에 그대로 — 요구사항은 그 결정에서 지금 고른 안의 것) · existing(지금 그대로 있는 동작이면 그 파일을 file 에) · needed(요청한 "
              "요구사항을 이루는 데 필요해 더하는 동작이면 그 번호를 for 에) 중 하나다. 요청에 없는 기능(다른 화면에 있는 검색·정렬 등)을 "
              "넣는 편이 낫다고 보면 계획의 decisions 에 '추가 제안: …'으로 내고 그 요구사항을 from=decision 으로 잇는다")


def _squash(text) -> str:
    """빈칸·문장 부호를 뺀 소문자 — 인용·이름을 견줄 때 띄어쓰기·따옴표 차이를 넘는다."""
    return re.sub(r"[\W_]+", "", str(text or "")).lower()


def req_origin(r: dict) -> dict:
    """요구사항 입력에서 출처 칸만 — {from, quote?, decision?, file?, for?, screen?}. 결정의 어느 안인지는 적지 않는다 — 요구사항은
    늘 계획에서 지금 고른 안(choice)의 것이고, 안 이름을 두 곳에 적게 하니 글자가 어긋나 막혔다 (실험 127·128)."""
    src = str(r.get("from") or "").strip().lower()
    out: dict = {"from": src} if src else {}
    for k, n in (("quote", 300), ("decision", 60), ("file", 200)):
        if str(r.get(k) or "").strip():
            out[k] = str(r[k]).strip()[:n]
    fr = r.get("for")
    ids = [str(x).strip() for x in (fr if isinstance(fr, list) else [fr] if fr else []) if str(x).strip()]
    if ids:
        out["for"] = ids[:6]
    if "screen" in r:
        v = r["screen"]
        out["screen"] = v if isinstance(v, bool) else str(v).strip().lower() not in ("false", "0", "no", "아니오", "아님", "")
    return out


def _origin_file(o: dict, root: str | Path | None = None) -> str:
    """출처 칸의 파일 — 대상 폴더 기준 경로로 (대상 폴더 안의 절대 경로도 받는다)."""
    raw = _path_part(o.get("file") or "")
    if root and raw:
        try:
            p = Path(raw)
            if p.is_absolute():
                raw = p.resolve().relative_to(Path(root).resolve()).as_posix()
        except (OSError, ValueError):
            pass
    return _rel(raw)


# Windows·macOS(APFS 기본) 는 파일 이름의 대소문자를 가리지 않는다 — App.tsx 와 app.tsx 는 같은 파일
_CASELESS_FS = sys.platform in ("win32", "darwin")


def _fold(x: str) -> str:
    import unicodedata
    x = unicodedata.normalize("NFC", x)
    return x.lower() if _CASELESS_FS else x


def _same_file(a: str, b: str) -> bool:
    return _fold(a) == _fold(b)


def origin_problems(run: dict, reqs: list[dict], user_rows: dict | None = None) -> list[str]:
    """요청사항 실행에서 요구사항을 저장할 때 — 출처 칸이 사실과 맞나 (구조와 사실만 본다). 걸린 것을 모두 돌려준다.
    user_rows: 사용자가 앱에서 쓴 요구사항 {번호: 글} — 그 글도 사용자의 말이고, needed 가 받쳐 줄 수 있다."""
    user_rows = user_rows or {}
    words = _squash(chr(10).join(_user_texts(run) + list(user_rows.values())))
    before = _files_at_start(run)
    before_l = {_fold(x) for x in before} if before is not None else before
    root = Path(run["target_root"])
    by_id = {str(r.get("id")): req_origin(r) for r in reqs if r.get("id")}
    by_id.update({k: {"from": "user"} for k in user_rows})
    out = []
    for i, r in enumerate(reqs):
        rid = str(r.get("id") or f"{i + 1}번째")
        o = req_origin(r)
        src = o.get("from")
        if src not in ORIGINS:
            out.append(f"{rid}: from 이 없거나 모르는 값")
        elif src == "request":
            q = _squash(o.get("quote"))
            if len(q) < 2:  # 길이로 거르지 않는다 — '삭제'·'수정'처럼 짧은 요청 낱말도 근거다 (실험: 4자 이상을 요구하니 '삭제'가 막혔다).
                # 낱말 하나로 다른 기능을 잇는지는 사용자가 카드에서 본다 (요구사항 옆에 근거 인용이 보인다)
                out.append(f"{rid}: from=request 인데 quote 가 없다")
            elif q not in words:
                out.append(f"{rid}: quote '{str(o.get('quote'))[:40]}' 가 사용자의 말에 그대로 없다")
        elif src == "decision":
            if not o.get("decision"):
                out.append(f"{rid}: from=decision 인데 decision(결정 이름)이 없다")
        elif src == "existing":
            f = _origin_file(o, root)
            if not f:
                out.append(f"{rid}: from=existing 인데 file 이 없다")
            elif (_fold(f) not in before_l) if before is not None else not (root / f).is_file():
                out.append(f"{rid}: file '{f}' 은(는) 작업을 시작할 때 있던 파일이 아니다")
        else:  # needed
            targets = o.get("for") or []
            if not targets:
                out.append(f"{rid}: from=needed 인데 for 가 없다")
            bad = [t for t in targets if (by_id.get(t) or {}).get("from") not in ("request", "decision", "user")]
            if bad:
                out.append(f"{rid}: for 의 {', '.join(bad)} 은(는) 이 목록에서 요청·결정에서 온 요구사항이 아니다")
    return out


def _req_meta(run: dict) -> dict:
    """저장한 요구사항의 출처 — {R1: {from, …}} (요청사항 실행에서 에이전트가 저장할 때 남긴다. 사용자가 앱에서 고친 요구사항은 없다)."""
    m = run.get("req_meta")
    return m if isinstance(m, dict) else {}


def _origin_plan_problems(picks: list[dict], steps: list[dict], meta: dict, req_ids: list[str]) -> list[str]:
    """요구사항의 출처가 계획과 맞물리나 — 기대는 결정이 계획에 있나, '지금 있는 동작'을 맡는 단계가 그 파일을 고치나 (새 파일만
    만드는 단계면 다른 화면을 본떠 새로 넣는 것이다). 글을 읽지 않고 이름·파일을 견준다."""
    topics = {_squash(d.get("topic")): d for d in picks}
    out = []
    for rid in req_ids:
        o = meta.get(rid) or {}
        if o.get("from") == "decision":
            d = topics.get(_squash(o.get("decision")))
            if d is None:
                out.append(f"{rid} 이(가) 기대는 결정 '{o.get('decision')}' 이 계획의 decisions 에 없다")
        elif o.get("from") == "existing":
            f = _origin_file(o)
            cover = [s for s in steps if rid in (s.get("requirements") or [])]
            if f and cover and not any(_same_file(_rel(x), f) for s in cover for x in s.get("files") or []):
                out.append(f"{rid} 은(는) 지금 있는 동작({f})이라고 했는데 그것을 맡는 단계가 그 파일을 고치지 않는다")
    return out


ORIGIN_PLAN_HOW = ("결정에 딸린 요구사항이면 그 결정을 계획의 decisions 에 넣는다 (decision 칸에 적은 이름 그대로). '지금 있는 동작'"
                   "(existing)을 맡는 단계는 그 파일을 고친다 — 다른 화면을 본떠 새로 넣는 것이면 지금 있는 동작이 아니라 요청에 없는 "
                   "추가다: decisions 에 '추가 제안: …'으로 내고 그 요구사항을 from=decision 으로 잇는다")


def _settled_decisions(prev: dict) -> dict:
    """The decisions the user already approved in this run — on an approved plan (the one being replaced, or one kept in its
    history). {squashed topic: decision}; a later approval wins. A decision stays the user's once approved: a later plan carries
    it without asking again (trial 144: the follow-up plan had to list the earlier decisions again and the card asked them
    again)."""
    out = {}
    for p in [*(prev.get("history") or []), prev]:
        if isinstance(p, dict) and p.get("approved"):
            for d in p.get("decisions") or []:
                if isinstance(d, dict) and d.get("topic") and d.get("choice"):
                    out[_squash(d["topic"])] = {k: v for k, v in d.items() if k not in ("shots", "looks", "reqs", "options")}
    return out


def _carry_settled(picks: list[dict], prev: dict, meta: dict) -> list[dict]:
    """Mark the decisions in this plan that the user already approved with the same choice (settled — the card shows them as
    decided, not to pick again), and return the approved decisions this plan left out that a requirement still rests on (the
    toolkit carries them; the agent does not have to repeat them)."""
    settled = _settled_decisions(prev)
    have = set()
    for d in picks:
        key = _squash(d["topic"])
        have.add(key)
        s = settled.get(key)
        if s and s.get("choice") == d["choice"]:
            d["settled"] = True
            if s.get("changed_by_user"):
                d["changed_by_user"] = True
    wanted = {_squash(o.get("decision")) for o in meta.values() if isinstance(o, dict) and o.get("from") == "decision"}
    return [{**s, "options": [], "settled": True} for k, s in settled.items() if k not in have and k in wanted]


def _link_decisions(picks: list[dict], meta: dict, req_ids: list[str]) -> None:
    """결정마다 그 결정에 딸린 요구사항 — 카드가 그 요구사항에 '고를 값'을 붙인다 (출처에 적힌 결정 이름으로 잇는다).
    실험: '제목 100자'를 결정으로 냈는데 요구사항에도 그대로 적혀, 사용자는 이미 정해진 값으로 읽었다."""
    for d in picks:
        hit = [rid for rid in req_ids if (meta.get(rid) or {}).get("from") == "decision"
               and _squash(meta[rid].get("decision")) == _squash(d.get("topic"))]
        if hit:
            d["reqs"] = hit[:10]
        else:
            d.pop("reqs", None)


def requirement_gate(run: dict, reqs: list[dict], user_ids: set | None = None) -> str | None:
    """계획을 낸 뒤에 요구사항을 바꿀 때 — 계획 때와 같은 기준(근거 없는 값·출처가 계획과 맞물리나·내부 표시)으로 본다. 실험: 승인 뒤
    구현하며 '제목 100자·내용 5000자'를 요구사항에 더했고 계획 때만 보던 검사를 지나갔다 (모의 사용자만 잡았다)."""
    plan = run.get("plan") or {}
    user_ids = user_ids or set()
    rows = [{"id": str(r.get("id") or f"#{i + 1}"), "text": str(r.get("text") or "")} for i, r in enumerate(reqs)
            if str(r.get("id") or "") not in user_ids]
    if run.get("mode") != "request":
        return None
    if not plan.get("steps"):
        # 첫 계획 전 — 값은 계획의 결정과 함께 본다. 번호는 지금 본다 (계획 때에야 알리면 요구사항부터 다시 냈다)
        jargon = _card_jargon(run, [(f"요구사항 {r['id']}", r["text"]) for r in rows], paths=False, idents=False)
        return ("요구사항에 사용자가 모르는 내부 표시가 있다: " + "; ".join(jargon[:4]) + " — 화면에서 보이는 것과 동작의 말로 쓴다"
                ) if jargon else None
    picks = plan.get("decisions") or []
    # 승인 전이면 계획의 결정으로 내고, 승인 뒤(구현 중)면 사용자에게 묻는다 — 어느 쪽이든 사용자가 고른 뒤에 요구사항에 넣는다
    how = ("사용자에게 묻고(wb_person_ask, 추천안과 함께) 답을 받은 뒤 넣는다" if plan.get("approved")
           else "계획의 decisions 에 추천안으로 넣어 계획을 먼저 다시 낸(wb_plan_set) 뒤 넣는다")
    problems = []
    meta = {**_req_meta(run), **{str(r["id"]): req_origin(r) for r in reqs if r.get("id") and req_origin(r)}}
    # 아직 계획에 없는 결정에 딸린 요구사항의 값은 그 결정을 넣은 계획을 낼 때 본다 — 순서가 요구사항 → 계획이라 여기서 막으면
    # 계획을 두 번 내야 했다 (결정만 넣은 계획 → 요구사항 → 그 요구사항을 맡긴 계획)
    topics = {_squash(d.get("topic")) for d in picks}
    pending = {k for k, o in meta.items() if o.get("from") == "decision" and _squash(o.get("decision")) not in topics}
    left = _unsourced_values(run, picks, [r for r in rows if r["id"] not in pending], meta)
    if left:
        problems.append("요구사항에 근거를 찾지 못한 값이 있다: " + "; ".join(left[:6]) + " — 요청·답·기존 코드·계획의 결정 어디에도 없다. "
                        "사용자가 정할 값이면 " + how + " (아직 계획에 없는 결정이면 요구사항을 from=decision 으로 먼저 저장하고 그 결정을 "
                        "넣은 계획을 낸다). " + VALUE_OTHER)
    # 아직 계획에 없는 결정을 가리키는 요구사항은 받는다 — 계획을 고칠 때 순서는 요구사항 → 계획이라, 새 결정에 딸린 요구사항이 먼저
    # 저장된다 (실험: 여기서 막으니 계획은 '없는 요구사항 번호'로, 요구사항은 '계획에 없는 결정'으로 서로 막았다). 그 결정이 계획에
    # 들어왔는지는 다음 계획 제출과 인계 때 본다
    plan_p = [x for x in _origin_plan_problems(picks, plan["steps"], meta, [r["id"] for r in rows]) if "decisions 에 없다" not in x]
    if plan_p:
        problems.append("요구사항의 출처가 계획과 맞물리지 않는다: " + "; ".join(plan_p[:6]) + " — " + ORIGIN_PLAN_HOW + ". 다른 안이 낫다고 보면 " + how)
    ui = bool(_screen_files(plan["steps"]))
    jargon = _card_jargon(run, [(f"요구사항 {r['id']}", r["text"]) for r in rows], paths=ui, idents=ui,
                          planned=[f for s in plan["steps"] for f in s.get("files") or []])
    if jargon:
        problems.append("요구사항에 사용자가 모르는 내부 표시가 있다: " + "; ".join(jargon[:4]) + " — 화면에서 보이는 것과 동작의 말로 쓴다")
    return " / ".join(problems) if problems else None


def _files_at_start(run: dict) -> set[str] | None:
    """시작 전 체크포인트에 있던 파일 목록 — 한 번에 (모르면 None)."""
    from beom_workbench.core import checkpoints, tasks
    cid = tasks._start_checkpoint(settings, run)
    if not cid:
        return None
    try:
        out = checkpoints._git(inbox.run_dir(settings, run["id"]) / "shadow.git", Path(run["target_root"]), "ls-tree", "-r",
                               "--name-only", cid, check=False).stdout
    except Exception:  # noqa: BLE001
        return None
    return {x.strip() for x in out.splitlines() if x.strip()}


def _shared_gate(run: dict, steps: list[dict], changes: list[dict] | None, prev: dict) -> tuple[list[dict], str | None, list[str]]:
    """기존 화면 쪽 공통 부품을 고치는 계획 — 신고(shared_changes: 파일·이유)가 있어야 한다. 사용자가 계획 카드에서 보고 승인한다.
    셋째 값: 공통 부품이 아니라 신고에서 뺀 파일 (그냥 고치면 된다)."""
    parts = run.get("shared_parts") or {}
    norm = lambda f: _path_part(f).replace("\\", "/").lstrip("./")  # noqa: E731
    root = Path(run["target_root"])
    style = lambda f: f.lower().endswith(STYLE_EXTS) and (root / f).is_file() and bool(run.get("existing"))  # noqa: E731
    if not parts and not any(style(norm(c.get("file", ""))) for c in changes or [] if isinstance(c, dict)):
        return [], None, []
    if changes is None:
        changes = prev.get("shared_changes") or []  # 다시 낸 계획 — 앞의 신고를 이어 받는다
    out, bad, dropped = [], [], []
    for c in changes or []:
        if not isinstance(c, dict) or not str(c.get("file", "")).strip():
            continue
        f, why = norm(c["file"]), str(c.get("why", "")).strip()
        if f in (run.get("shared_refused") or []):
            bad.append(f"{f} — 사용자가 이 공통 부품의 수정을 이미 거절했다. 신고하지 말고 새 화면 쪽에서 해결한다")
        elif f not in parts and not style(f):
            # 공통 부품이 아닌 파일을 신고한 것은 해가 없다 — 돌려보내면 그것만 빼고 다시 내느라 한 번을 더 쓴다 (실험: 세 실행에서
            # 매번). 툴킷이 빼고 알린다 (계획 카드에 '공통 부품 수정'으로 잘못 보이지 않게)
            dropped.append(f)
        elif not why:
            bad.append(f"{f} — why(왜 꼭 고쳐야 하는지, 다른 화면에 무엇이 달라지는지)가 비어 있음")
        else:
            out.append({"file": f, "why": why[:300], "users": parts.get(f, 0)})  # 0: 공통 스타일 (쓰는 화면 수를 세지 않는다)
    if bad:
        return [], "shared_changes 를 고친다: " + "; ".join(bad), dropped
    declared = {c["file"] for c in out}
    touch = sorted({norm(f) for s in steps for f in s["files"]} & set(parts) - declared)
    if touch:
        return [], ("계획이 다른 화면들이 쓰는 기존 공통 부품을 고친다: " + ", ".join(f"{f}({parts[f]}곳)" for f in touch[:6])
                    + " — 공통 부품은 고치지 않고 새 화면 쪽에서 해결하도록 steps 를 바꾼다. 꼭 고쳐야 하면 shared_changes=[{file, why}] 로 "
                      "신고한다 (사용자가 계획 카드에서 보고 승인해야 고칠 수 있다)"), dropped
    return out, None, dropped


def _layout_gate(run: dict, steps: list[dict], basis: str) -> list[str]:
    """새 프로젝트의 폴더 구조 — 계획한 파일과 지금 만든 파일을 언어별 정석 구조와 견준다. 기존 코드가 있던 폴더는 그 구조를 따르므로
    보지 않는다. 사용자가 구조를 정해 주었으면(layout_basis) 보지 않는다. 기획 모드는 코드를 만들지 않으니 보지 않는다."""
    from beom_workbench.core import runner as runner_core, scaffold
    if basis.strip() or run.get("mode") not in ("request", "reference") or not runner_core.new_project(run):
        return []
    planned = [f for s in steps for f in s["files"]]
    return scaffold.check(planned, scaffold.files_now(Path(run["target_root"])))


def _plan_scope(run: dict | None, steps: list[dict], picks: list[dict], given: str, prev: dict) -> tuple[str | None, str | None]:
    """이 계획의 디자인 범위 — 기존 화면이 있는 제품에서 화면·스타일 파일을 고치는 계획일 때만. 사용자가 카드에서 직접 고른 범위가
    먼저, 그다음 에이전트가 design_scope 칸에 적은 값. 칸이 비었으면 돌려보낸다 (요청의 말로 짐작해 채우지 않는다). 범위는 늘 카드에
    '디자인 범위' 결정으로 넣어 사용자가 고르게 한다 (picks 를 고친다). 돌려주는 것: (범위, "missing" 또는 None)."""
    from beom_workbench.core import runner as runner_core
    if run is None or run.get("mode") != "request" or not _screen_files(steps) or not runner_core.screens_at_start(run):
        return None, None
    mine = next((d for d in prev.get("decisions") or [] if d.get("kind") == "design_scope" and d.get("changed_by_user")), None)
    user = _ds.scope_of(mine.get("choice")) if mine else None
    agent = _ds.normalize(given)
    if not user and not agent:
        # 칸을 비웠다 — 요청의 말로 짐작해 채우지 않는다. 기존 화면을 고치는 계획이면 늘 적어야 하는 칸이다 (규칙도 그렇게 말한다)
        return None, "missing"
    scope = user or agent
    picks[:] = [d for d in picks if d.get("kind") != "design_scope" and str(d.get("topic", "")).strip() != _ds.SCOPE_TOPIC_LABEL]
    d = _ds.scope_decision(scope)  # 늘 카드에 — 범위를 정하는 것은 사용자다
    if user:
        d["changed_by_user"] = True
    picks.insert(0, d)
    return scope, None


def _design_gate(run: dict, steps: list[dict], picks: list[dict], look: list[str], basis: str, scope: str | None = None) -> str | None:
    """새로 만드는 화면의 디자인 방향 — 참고 화면을 찍어 보고(조사), 방향이 다른 시안을 그림으로 보여 사용자가 고르게 한다.
    규칙에 '반드시'라고 적어도 같은 에이전트가 어떤 때는 하고 어떤 때는 건너뛰었다 — 계획을 받을 때 확인한다.
    빼는 경우: 화면이 없는 작업, 기존 화면이 여럿 있는 프로젝트에서 디자인을 그대로 두는 작업(scope 가 restyle·redesign 이 아님),
    사용자가 정한 디자인(design_basis), 화면 엔진이 없는 설치. 참고 화면은 웹 접속이 규칙상 막혔거나 사용자가 거절했으면 뺀다.
    scope: 이 계획의 디자인 범위 (계획의 design_scope 칸·사용자가 카드에서 고른 것 — 글을 읽어 짐작하지 않는다)."""
    from beom_workbench.core import runner as runner_core, screens
    if run.get("mode") == "reference":
        return None  # 원본이 화면의 모습을 정한다 — 시안을 고르지 않고 검증 때 원본과 견준다 (다른 이식 도구들도 시안 단계가 없다)
    ui = _screen_files(steps)
    design = _design_pick(picks)
    if not (ui or design) or not screens.available():
        return None
    existing = runner_core.screens_at_start(run)
    if existing and scope not in ("restyle", "redesign"):
        return None  # 기존 제품에 기능을 더한다 — 기존 디자인을 따른다 (follows·디자인 점검의 compare 가 지킨다)
    if not existing:
        scope = None
    # 디자인을 바꾸는 작업에서 design_basis 는 시안을 건너뛰는 근거가 못 된다 — 실험: '따뜻하게 바꿔 줘'라는 요청 문장을 근거로 적고
    # 시안 없이 바로 바꿨다. 사용자가 브랜드·디자인 자료를 첨부했을 때만 그것을 따른다
    problems = []
    broken = [x for d in picks for x in d.get("sian_broken") or []]
    if broken:  # 붙인 시안은 사용자에게 보인다 — 근거(design_basis)가 있어도 깨진 시안은 고친다
        problems.append("시안이 PC 화면에서도 깨진다 — 고쳐서 다시 낸다: " + " · ".join(broken[:6]))
    tells = [x for d in picks for x in d.get("sian_tells") or []]
    if tells:
        # 사용자가 고른 시안이 곧 구현의 출발점이다 — 시안의 AI 티는 그대로 제품이 된다 (실험 147). 취향이라 막지 않고 처음 한 번만 돌려보낸다
        import hashlib
        tsig = hashlib.sha1(json.dumps(sorted(tells), ensure_ascii=False).encode()).hexdigest()[:12]
        if run.get("look_tell_sig") != tsig:
            inbox.update_run(settings, run["id"], lambda r: r.update(look_tell_sig=tsig))
            problems.append("시안에 AI 티로 읽히는 것이 있다 (사용자에게 보이기 전에 한 번 알린다): " + " · ".join(tells[:6])
                            + " — 적힌 대로 바꿔 다시 낸다. 분야에 꼭 맞아 일부러 쓴 것이면 그대로 다시 내도 된다")
    if basis.strip() and not (scope and not runner_core._attachments(run)):
        if _basis_found(run, basis):
            return ("디자인 방향을 정하기 전에: " + problems[0]) if problems else None
        problems.append("design_basis 의 근거를 찾지 못했다 — 사용자가 정해 준 디자인이면 요청·답의 그 말을 그대로, 따를 파일·첨부면 그 경로를 "
                        "적는다 (없으면 아래대로 시안을 붙인다)")
    if run.get("depth") == "quick":  # '간단히' — 사용자가 조사·시안 없이 빨리 만들기를 골랐다 (깨진 시안·AI 티 알림은 그대로)
        return ("디자인 방향을 정하기 전에: " + " / ".join(problems)) if problems else None
    evs = [e.get("kind") for e in inbox.events(settings, run["id"])[0]]
    if scope != "restyle" and inbox.decide(run, "web") != "refuse" and not ({"design_refs", "design_refs_declined", "design_refs_failed"} & set(evs)):
        problems.append("참고 화면을 아직 보지 않았다 — mcp__beom-workbench__wb_design_refs 로 같은 분야의 잘 만든 서비스 2~4곳을 찍어 "
                        "보고 (사용자가 거절하면 그대로 진행)")
    shots = (design or {}).get("shots") or {}
    # 다시 낸 계획 — 앞서 시안을 그림으로 보여 줬으면(사용자가 이미 보고 골랐으면) 다시 붙이라고 하지 않는다
    seen_before = max((len(d.get("shots") or {}) for d in ((run.get("plan") or {}).get("decisions") or [])), default=0)
    need = 3 if run.get("depth") == "deep" and not scope else 2  # '깊게' 새로 만드는 화면은 시안 3개
    if len(shots) < need and seen_before < need and not any(d.get("looks_failed") for d in picks):
        problems.append(f"'디자인 방향' 결정에 방향이 확실히 다른 시안을 선택지 {need}개 이상 그림으로 붙여야 한다 — 핵심 화면 한 장씩 "
                        ".beom/looks/a.html 처럼 만들고 decisions 의 그 결정에 looks=[\".beom/looks/a.html\", \".beom/looks/b.html\"] "
                        "(choice, options 순서대로 시안 경로만)" + ("" if design else " — 그 결정이 아직 없다"))
    if not problems:
        return None
    if scope == "restyle":
        problems.append("느낌만 바꾸는 작업이니 시안은 기존 화면을 그대로 옮겨 배치·부품·문구는 두고 색·글꼴·모서리·간격·그림자 같은 "
                        "디자인 토큰만 바꾼 것으로 만든다")
    elif scope == "redesign":
        problems.append("다시 디자인하는 작업이니 시안은 기존 화면의 기능·문구·데이터를 그대로 담고 모습을 새 방향으로 만든다")
    return (("디자인 방향을 정하기 전에: " if scope else "새로 만드는 화면의 디자인을 정하기 전에: ") + " / ".join(problems) +
            (". 사용자가 브랜드·디자인 자료를 첨부했으면 design_basis 에 그 자료를 적는다" if scope else
             ". 사용자가 브랜드·디자인을 이미 정해 주었으면 design_basis 에 근거를 적는다"))


def _basis_found(run: dict, basis: str) -> bool:
    """design_basis 가 가리키는 것이 있나 — 사용자의 말에 그대로 있는 6자 이상의 부분, 대상 폴더에 있는 파일, 첨부 이름 (뜻은 보지 않는다)."""
    from beom_workbench.core import runner as runner_core
    root = Path(run["target_root"])
    for m in re.finditer(r"[\w./\-]+\.[A-Za-z0-9]{1,5}", basis):
        if (root / _path_part(m.group(0))).exists():
            return True
    names = {Path(str(a)).name.lower() for a in runner_core._attachments(run)}
    if any(n and n in basis.lower() for n in names):
        return True
    b, words = _squash(basis), _squash(chr(10).join(_user_texts(run)))
    return any(b[i:i + 6] in words for i in range(max(0, len(b) - 5)))


SIAN_BROKEN = ("가로로 넘침", "글자가 잘림", "한두 글자씩 줄바뀜", "누르는 곳끼리 겹침")


def _shoot_looks(run: dict, picks: list[dict]) -> list[str]:
    """디자인 방향 시안(HTML)을 찍어 결정에 붙인다 — {"선택지": ".beom/looks/a.png"}. 시안은 대상 폴더의 .beom 안 HTML 만.
    찍을 때 디자인 점검도 한다 — 사용자는 시안 그림을 보고 고른다. 카드 그림은 휴대폰 너비(390px) 첫 화면이고, 깨짐(가로 넘침·글자
    쪼개짐)은 PC 너비(1280px)에서 잰다 — 그 너비에서도 깨진 시안은 '방향'이 아니라 깨진 화면이라 결정에 sian_broken 으로 남겨(계획을
    받지 않는다) 고쳐 다시 내게 한다. 휴대폰 너비에서만 깨지는 것은 알리기만 한다 — 그 제품이 휴대폰을 지원하는지는 글을 읽어
    짐작하지 않는다 (실험: 관리자 도구 시안을 휴대폰 너비로만 재 '깨짐'으로 계획이 막혔다; 실제 화면의 휴대폰 점검은 구현 뒤
    wb_design_check 가 한다)."""
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
            for mobile, name in ((True, f"look-{i + 1}.png"), (False, f"look-{i + 1}-pc.png")):
                spec = {"file": f.relative_to(root).as_posix(), "mobile": mobile, "wait_ms": 600, "audit": True}
                pg = screens.page_spec(spec, None, root, str(out_dir / name), file_base=srv.base)
                pg["full_page"] = False  # 첫 화면 그대로 — 카드 안에서 비교하기 좋게
                pages.append(pg)
        try:
            got = screens.capture(pages)
        except ToolError as e:
            for d, _, _ in jobs:
                d["looks_failed"] = True  # 화면 엔진이 찍지 못했다 — 시안은 글로만 (그림 2장을 요구하면 계획이 영영 막힌다)
            return errors + [e.message]
    for i, (d, label, f) in enumerate(jobs):
        phone, pc = got[2 * i], got[2 * i + 1]
        if phone.get("ok"):
            d.setdefault("shots", {})[label] = Path(phone["out"]).resolve().relative_to(root).as_posix()
        else:
            errors.append(f"시안을 찍지 못함: {f.name} {(phone.get('errors') or [''])[0]}")
        for r, size, blocks in ((pc, "PC 1280px", True), (phone, "휴대폰 390px", False)):
            fix, adv = _design_findings(r.get("audit")) if r.get("ok") and r.get("audit") else ([], [])
            if blocks:
                d.setdefault("sian_tells", []).extend(f"{f.name}: {x[5:]}" for x in adv
                                                      if x.startswith("AI 티") and not any(TELLS[k] in x for k in PAGE_ONLY_TELLS))
            broken = [x for x in fix if any(k in x for k in SIAN_BROKEN)]
            if broken and blocks:
                d.setdefault("sian_broken", []).extend(f"{f.name} ({size}): {x}" for x in broken[:4])
            elif broken:
                errors.append(f"참고 — 시안 {f.name} 이(가) {size} 에서 깨져 보임: " + " / ".join(broken[:2]))
    return errors


WEB_READ = "web_read"  # 툴킷이 하는 공개 페이지 읽기(문서 받기·참고 화면) — 한 번 허락하면 그 작업 동안
WEB_READ_NOTE = "허용하면 이 작업 동안 공개 페이지 읽기는 다시 묻지 않습니다"
URL_RX = re.compile(r"https?://[^\s\"'<>)\]]+")


def _opened_urls(run: dict) -> set[str]:
    """이 실행에서 실제로 연 웹 페이지 — 툴킷이 받은 문서, 찍은 참고 화면, 에이전트가 연 페이지(WebFetch). 에이전트의 말이 아니라 기록."""
    out: set[str] = set()
    for e in inbox.events(settings, run["id"])[0]:
        k = e.get("kind")
        if k == "web_doc" and e.get("url"):
            out.add(str(e["url"]))
        elif k == "design_refs":
            out.update(str(u) for u in e.get("urls") or [])
        elif k == "agent":
            for it in e.get("items") or []:
                if str(it.get("tool") or "").split("__")[-1] in ("WebFetch", "web_fetch", "fetch"):
                    out.update(URL_RX.findall(str(it.get("input") or "")))
    return out


def _host(u: str) -> str:
    from urllib.parse import urlsplit
    h = urlsplit(u).netloc.lower()
    return h[4:] if h.startswith("www.") else h


RESEARCH_AREAS = ("기능", "사용 흐름·접근성", "연동·API", "데이터", "성능·효율", "비용·유지보수", "보안·개인정보", "운영·오류·검증")
_AREA_ALIAS = {"운영오류": "운영·오류·검증", "사용흐름": "사용 흐름·접근성", "접근성": "사용 흐름·접근성", "비용": "비용·유지보수", "유지보수": "비용·유지보수"}
DOMAIN_PREFIX = "분야"  # 분야 특화 영역 — '분야·정산', '분야·좌표계'처럼 에이전트가 이름을 붙인다


def _area(a) -> str:
    """영역 이름 — 가운뎃점·공백·쉼표 차이는 같은 영역으로 (뜻은 보지 않는다)."""
    key = re.sub(r"[\s·・,./]+", "", str(a or ""))
    hit = next((x for x in RESEARCH_AREAS if re.sub(r"[\s·・,./]+", "", x) == key), None) or _AREA_ALIAS.get(key)
    return hit or str(a or "").strip()[:30]


def _compare_missing(picks: list[dict], prev: dict) -> list[str]:
    """선택지가 있는 결정 — 추천과 대안마다 좋은 점·아쉬운 점이 있나 (칸이 찼는지만 본다). 다시 낸 계획이 비운 비교는 앞 계획에서
    같은 결정·같은 선택지의 것을 이어 받는다. 앞에서 정한 결정·툴킷이 붙이는 디자인 범위·시안으로 고르는 디자인 방향은 뺀다."""
    old = {d.get("topic"): d.get("compare") for d in (prev or {}).get("decisions") or [] if d.get("compare")}
    out = []
    for d in picks:
        names = [d.get("choice"), *(d.get("options") or [])]
        if d.get("settled") or d.get("kind") == "design_scope" or d.get("looks") or d.get("shots") or len(names) < 2:
            continue
        if not d.get("compare") and old.get(d.get("topic")) and {c.get("option") for c in old[d["topic"]]} >= set(names):
            d["compare"] = old[d["topic"]]
        have = {c.get("option") for c in d.get("compare") or [] if c.get("pros") and c.get("cons")}
        miss = [n for n in names if n not in have]
        if miss:
            out.append(f"'{d.get('topic')}' ({', '.join(str(m)[:30] for m in miss[:3])})")
    return out


def _research_items(research) -> list[dict]:
    out = []
    for r in research or []:
        if isinstance(r, dict) and str(r.get("finding") or "").strip():
            out.append({"area": _area(r.get("area")), "finding": " ".join(str(r["finding"]).split())[:500],  # 확인한 것·정한 것·남은 위험
                        "source": str(r.get("source") or "").strip()[:500]})
    return out[:48]


def _research_problems(run: dict, found: list[dict]) -> str | None:
    """'깊게' 실행의 조사 — 알게 된 것마다 실제로 연 페이지가 출처인가, 서로 다른 곳 3곳 이상인가. 뜻은 보지 않는다 (사실만)."""
    opened = _opened_urls(run)
    hosts_open = {_host(u) for u in opened}
    unopened = [r["source"] for r in found if r["source"] and r["source"] not in opened and _host(r["source"]) not in hosts_open]
    hosts = {_host(r["source"]) for r in found if r["source"] and r["source"] not in unopened}
    if run.get("depth") == "deep":
        missing = [a for a in RESEARCH_AREAS if not any(r["area"] == a for r in found)]
        # 이 분야만의 것 — 고정 영역 밖의 영역을 하나 이상, 없으면 그렇다고 (뜻은 보지 않는다, 칸만)
        if not any(r["area"].startswith(DOMAIN_PREFIX) for r in found):
            missing.append("분야 특화(이 분야만의 규정·관행·함정 — area 를 '분야·이름'으로; 없으면 area '분야', finding '없음 — 까닭')")
        # 더 찾지 못했다고 밝혔으면 그것이 결과다 — 숫자를 채우려고 관련 없는 페이지를 열게 하지 않는다
        few = any(r["area"] == "출처" and re.match(r"\s*\d+\s*곳뿐", r["finding"]) for r in found)
        if missing or (len(hosts) < 3 and not few):
            return ("깊게 하는 작업이라 설계 검토가 먼저다 — " + ("검토하지 않은 영역: " + ", ".join(missing) + " (해당하지 않으면 finding 에 "
                    "'해당 없음 — 까닭'). " if missing else "") + ("바깥 사실은 서로 다른 출처 3곳 이상을 직접 열어(WebFetch 또는 "
                    "mcp__beom-workbench__wb_fetch_doc) 확인한다 (지금 확인된 출처 " + str(len(hosts)) + "곳). 찾아봤는데 더 없으면 억지로 "
                    "채우지 말고 {area: '출처', finding: 'N곳뿐 — 찾아본 곳과 없던 까닭'} 을 붙인다. " if len(hosts) < 3 and not few else "")
                    + "research=[{area, finding, source}] 로 계획에 붙인다")
    if unopened:
        return ("research 의 출처를 고친다 — 이 실행에서 열지 않은 주소: " + ", ".join(unopened[:4]) + " (직접 열어 본 페이지만 출처로 쓴다 — "
                "바깥 사실이 아니라 이 프로그램에서 정한 것이면 source 를 비운다)")
    return None


MAX_DECISIONS = 10  # 계획 카드에 싣는 결정 수
MAX_OPTIONS = 3     # 결정마다 추천 말고 대안 수


@mcp.tool()
@_record_refusal
def wb_plan_set(steps: list[dict], summary: str = "", design: str = "", decisions: list[dict] | None = None,
                 look: list[str] | None = None, previews: list[str] | None = None, follows: list[dict] | None = None,
                 design_basis: str = "", rules_checked: list[str] | None = None, layout_basis: str = "",
                 shared_changes: list[dict] | None = None, design_scope: str = "", research: list[dict] | None = None) -> dict:
    """구현 계획을 낸다 — 승인을 기다리는 실행이면 사용자가 앱에서 승인한 뒤 구현한다 (끝까지 맡김·계획 승인 끔은 이어서 구현). steps: [{title, files?: [...], commands?: [...], requirements: ["R1"]}]
    — 요구사항은 모두 어느 단계가 맡는다 (그대로 두는 동작 from=existing 은 빼도 된다. 준비·마무리 단계는 비운다).
    결정은 10개, 결정마다 대안 3개까지 (넘으면 돌려보낸다). 다시 낸 계획에서 비운 칸은 앞 계획의 것을 이어 받는다.
    research: 설계 검토 [{area: 기능|사용 흐름·접근성|연동·API|데이터|성능·효율|비용·유지보수|보안·개인정보|운영·오류·검증|분야·이름,
    finding: "확인한 것(어디서)·정한 것·남은 위험", source: "이 실행에서 직접 연 페이지 URL (바깥 사실일 때)"}] — 작업 깊이가 '깊게'면 여덟 영역과
    분야 특화(area '분야·이름' 하나 이상, 정말 없으면 area '분야' · finding '없음 — 까닭')가 모두 있고(해당 없으면 '해당 없음 — 까닭'), 바깥 사실의
    출처가 서로 다른 3곳 이상(더 없으면 area '출처' · finding 'N곳뿐 — 찾아본 곳')이어야 계획을 받는다 (툴킷이 연 기록과 맞춰 본다). 사용자가
    계획 카드에서 영역별로 본다.
    decisions: 사전 설계 결정 [{topic: "데이터 저장", choice: "SQLite 파일", options: ["JSON 파일"], why: "한 줄"}] — 형태(웹·앱·API)·
    데이터 저장·스택·실행 방법·디자인 방향처럼 결과를 크게 가르는 것. 사용자가 승인하며 대안으로 바꿀 수 있다.
    결정의 글(topic·choice·options·why)은 사용자의 말로 — 근거가 된 기존 파일은 basis: ["app/members/repo.py"] 에 (카드에 보이지 않는다).
    걸린 검사는 모두 한 번에 돌려준다 (problems) — 모두 고쳐 한 번에 다시 낸다.
    look: 디자인 컨셉 1~2줄, 전문 용어 없이 (사용자용). 색·글꼴 같은 수치는 계획에 쓰지 말고 구현의 토큰 파일에.
    시안: 디자인 방향 결정에 looks=[".beom/looks/a.html", ".beom/looks/b.html"] — choice, options 순서대로 그 안의 시안 경로만 — 를 주면
    툴킷이 각 시안(핵심 화면 한 장)을 찍어 계획 카드의 선택지 옆에 그림으로 보여 준다 — 사용자가 보고 고른다.
    follows: 기존 화면이 있는 프로젝트에서 화면을 만들·고칠 때 — 다른 화면들에서 확인한 규칙과 근거
    [{rule: "목록에서 상세는 행 더블클릭", evidence: ["src/pages/OrderList.jsx:8", "src/pages/UserList.jsx:8"], code: ["onRowDoubleClick"]}].
    rule 은 사용자의 말로 (카드에 보인다), 그 규칙이 가리키는 부품·이벤트·옵션 이름은 code 에 (카드에 보이지 않는다). 근거는 지금 있는
    코드의 파일:줄이어야 하고 (툴킷이 확인한다), 규칙 전체에 걸쳐 서로 다른 기존 파일 2개 이상. 사용자가 계획 카드에서 본다.
    rules_checked: 지시의 [작업 규칙·정책] 에 나온 문서 중 읽은 것 ["../../AGENTS.md", "docs/conventions.md"] — 에이전트 지시·규칙
    허브는 모두, 정책·가이드 문서는 이번 작업에 닿는 것을 (툴킷이 확인한다). 그 문서에서 따를 조항은 follows 의 근거(문서:줄)로.
    새로 만드는 화면이면 '디자인 방향' 결정에 시안(looks)을 선택지 2개 이상 붙여야 하고, 그 전에 참고 화면(wb_design_refs)을
    찍어 봐야 한다 — 툴킷이 확인한다. 사용자가 브랜드·디자인을 정해 주었으면 design_basis 에 그 근거(요청·첨부의 어디)를 적는다.
    shared_changes: 다른 화면들이 쓰는 기존 공통 부품([고치지 않을 공통 부품] 줄)을 꼭 고쳐야 할 때만 [{file: "src/components/BaseGrid.vue",
    why: "목록에 체크칸 이름을 붙이려고 — 다른 화면 7곳도 함께 바뀜"}]. 사용자가 계획 카드에서 보고 승인해야 그 파일을 고칠 수 있다.
    새 프로젝트(빈 폴더)면 steps.files 에 설정·테스트 파일까지 적는다 — 툴킷이 언어별 정석 구조와 견주어 분명히 벗어난 곳을 알린다.
    사용자가 구조·파일 이름을 정해 주어 다르게 하면 layout_basis 에 그 근거(요청의 어느 말)를 적는다 (계획 카드에 보인다).
    design_scope: 기존 화면이 있는 제품에서 화면·스타일 파일을 고치는 계획이면 디자인을 어디까지 바꾸는지 — "keep"(기존 디자인 그대로,
    기능을 더하는 작업) · "restyle"(배치·부품·문구는 두고 색·글꼴·모서리 같은 디자인 토큰만) · "redesign"(기능·문구는 두고 모습을 새로).
    그런 계획이면 꼭 적는다 (비우면 돌려보낸다 — 툴킷은 요청의 말로 짐작해 채우지 않는다). 계획 카드에 '디자인 범위'
    결정으로 보이고 사용자가 고른다.
    계획을 다시 내면 이전 승인은 풀린다."""
    clean = [{"title": str(s.get("title", "")).strip()[:200], "files": [str(f) for f in s.get("files") or []][:20],
              "commands": [str(c) for c in s.get("commands") or []][:10], "requirements": s.get("requirements") or [],
              "status": "todo"} for s in steps if str(s.get("title", "")).strip()]
    if not clean:
        return {"ok": False, "error": "단계가 비어 있음"}
    picks, over = [], []
    for d in decisions or []:
        if not isinstance(d, dict) or not str(d.get("topic", "")).strip() or not str(d.get("choice", "")).strip():
            continue
        # '[추천] …' — 카드가 추천 표시를 따로 붙인다. 글에 남기면 고른 뒤에도 '[추천]'이 승인 기록에 남는다
        bare = lambda x: _REC_TAG.sub("", str(x)).strip()[:120]  # noqa: E731
        choice = bare(d["choice"])
        all_opts = [bare(o) for o in d.get("options") or [] if bare(o) and bare(o) != choice]
        opts = all_opts[:MAX_OPTIONS]
        topic = str(d["topic"]).strip()[:60]
        if len(all_opts) > MAX_OPTIONS:  # 조용히 자르지 않는다 — 잘린 안에 기대는 요구사항이 뒤에서 '없는 안'으로 돌아왔다
            over.append(f"결정 '{topic}' 의 대안이 {len(all_opts)}개 — 추천 1개와 대안 {MAX_OPTIONS}개까지만 카드에 실린다")
        if isinstance(d.get("looks"), list):  # 안 순서대로 경로만 — [추천의 시안, 대안1의 시안, …] (안 이름을 다시 적지 않는다)
            d = {**d, "looks": {k: str(v) for k, v in zip([choice, *opts], d["looks"]) if str(v or "").strip()}}
        if isinstance(d.get("looks"), dict):
            d = {**d, "looks": {bare(k): v for k, v in d["looks"].items()}}
        item = {"topic": topic, "choice": choice, "options": opts, "why": str(d.get("why", "")).strip()[:300]}
        if isinstance(d.get("looks"), dict):
            item["looks"] = {str(k).strip()[:120]: str(v) for k, v in d["looks"].items() if str(k).strip()[:120] in [choice, *opts]}
            odd = [str(k) for k in d["looks"] if str(k).strip()[:120] not in [choice, *opts]]
            if odd:  # 이름이 다른 시안을 버리고 '시안이 없다'고 돌려보내면 무엇이 틀렸는지 알 수 없다
                over.append(f"결정 '{topic}' 의 looks 이름 {', '.join(repr(x) for x in odd[:3])} 이(가) choice·options 와 다르다 — "
                            "선택지 이름을 똑같이 적는다 (지금 선택지: " + ", ".join([choice, *opts]) + ")")
        # basis: 이 결정의 근거 파일(기존 코드의 같은 방식 등) — 카드의 글은 사용자의 말로 두고 파일은 여기에 (실험: 결정의 why 에
        # 파일 경로를 적어 계획이 돌아온 실행이 다섯)
        raw = d.get("basis")
        basis = [str(x).strip()[:120] for x in (raw if isinstance(raw, list) else [raw] if raw else []) if str(x).strip()][:6]
        if basis:
            item["basis"] = basis
        if isinstance(d.get("compare"), list) and d["compare"]:
            # 선택지마다 한 줄 — 무엇이 좋고 나쁜지, 비용·난이도, 근거 페이지. 이름이 선택지와 같아야 카드에서 그 선택지 옆에 붙는다
            rows = []
            for c in d["compare"][:MAX_OPTIONS + 1]:
                if not isinstance(c, dict):
                    continue
                name = bare(c.get("option") or "")
                rows.append({"option": name, **{k: " ".join(str(c.get(k) or "").split())[:200]
                                                for k in ("pros", "cons", "cost", "effort", "source") if str(c.get(k) or "").strip()}})
            wrong = [r["option"] for r in rows if r["option"] not in [choice, *opts]]
            if wrong:
                over.append(f"결정 '{topic}' 의 compare 이름 {', '.join(repr(x) for x in wrong[:3])} 이(가) choice·options 와 다르다 — "
                            "선택지 이름을 똑같이 적는다 (지금 선택지: " + ", ".join([choice, *opts]) + ")")
            item["compare"] = [r for r in rows if r["option"] in [choice, *opts]]
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
    # 다시 낸 계획 — 비운 칸은 앞의 계획에서 이어 받는다 (칸마다 달리 이어 받으니 글만 고쳐 다시 낸 계획이 엉뚱한 검사에 걸렸다)
    if rules_checked is None and prev.get("rules_checked"):
        rules_checked = list(prev["rules_checked"])
    if follows is None and prev.get("follows"):
        follows = [dict(x) for x in prev["follows"]]
    if not design_basis.strip() and prev.get("design_basis"):
        design_basis = prev["design_basis"]
    if not str(design_scope or "").strip() and prev.get("design_scope"):
        design_scope = prev["design_scope"]
    old_design = _design_pick(prev.get("decisions") or [])
    # 다시 낸 계획 — 시안을 다시 붙이지 않았으면 같은 주제의 결정에 앞서 찍은 그림을 잇는다 (주제 이름을 그대로 견준다)
    new_design = _design_pick(picks) or (next((d for d in picks if old_design and _squash(d["topic"]) == _squash(old_design.get("topic"))), None))
    if old_design and new_design and not new_design.get("looks") and old_design.get("shots"):
        keep = [new_design["choice"], *new_design["options"]]
        carried = {k: v for k, v in old_design["shots"].items() if k in keep}
        if carried:
            new_design["shots"] = carried
    # 돌려보낼 것은 모두 모아 한 번에 알린다 (_refusal) — 하나씩 알리면 고쳐 낼 때마다 다음 검사에 걸렸다
    problems: list[str] = list(over)
    refuse_extra: dict = {}
    scope, scope_note = _plan_scope(run, clean, picks, design_scope, prev)
    carried = _carry_settled(picks, prev, _req_meta(run) if run is not None else {})
    open_n = sum(1 for d in picks if not d.get("settled"))
    if open_n > MAX_DECISIONS:
        problems.append(f"결정이 {open_n}개다 (툴킷이 넣은 '디자인 범위' 포함) — 카드에는 {MAX_DECISIONS}개까지 실린다. 결과가 크게 "
                        "갈리는 것만 남기고, 이미 정해진 것(기존 프로젝트의 형태·스택 등)은 빼고, 비슷한 값은 한 결정으로 묶는다")
    follows_scope = scope
    if scope_note == "missing":
        scope_note = None
        follows_scope = "missing"  # 범위부터 정한다 — 따른 근거는 범위가 '그대로'일 때만 묻는다
        problems.append("기존 화면이 있는 제품의 화면·스타일 파일을 고치는 계획인데 design_scope 가 비었다 — \"keep\"(기존 디자인 그대로·기능을 "
                        "더하는 작업) · \"restyle\"(배치·부품·문구는 두고 디자인 토큰만) · \"redesign\"(기능·문구는 두고 모습을 새로) 중 하나를 "
                        "적는다. 카드에 '디자인 범위'로 실려 사용자가 고른다")
    rules, rule_errors, conv = _follows(run, clean, follows, follows_scope)
    if rules is None:
        problems += rule_errors
        rules = []
        if conv is not None:
            refuse_extra["conventions"] = conv
    read, read_error = _rules_checked(run, rules_checked, rules)
    if read_error:
        problems.append(read_error)
    look_errors = _shoot_looks(run, picks) if run is not None else []
    gate = _design_gate(run, clean, picks, lines, design_basis, scope) if run is not None else None
    if gate:
        problems.append(gate)
        if look_errors:
            refuse_extra["look_errors"] = look_errors
    unsourced = _unsourced_values(run, picks + carried) if run is not None and run.get("mode") == "request" else []
    if unsourced:
        problems.append("요구사항에 근거를 찾지 못한 값이 있다: " + "; ".join(unsourced[:6]) + " — 요청·이어서 보낸 말·질문의 답·"
                "첨부·기존 코드 어디에도 없는 값이다. " + VALUE_DECIDE + ". " + VALUE_OTHER)
    req_snap = []  # 이 계획을 낸 때의 요구사항 — 다시 낸 계획에서 무엇이 더해지고 바뀌고 빠졌는지 카드에 보인다
    if run is not None and run.get("mode") in ("request", "reference"):
        fields = [("요약", summary)] + [(f"{i + 1}단계 제목", s["title"]) for i, s in enumerate(clean)]
        fields += [(f"결정 '{d['topic'][:20]}'", " / ".join([d["topic"], d["choice"], *d["options"]])) for d in picks]
        why_fields = [(f"결정 '{d['topic'][:20]}' 의 까닭", d["why"]) for d in picks if d.get("why")]  # 제품·기술 이름을 써도 되는 칸
        fields += [("디자인 컨셉", x) for x in lines]
        from beom_workbench.core import runner as runner_core
        ui = runner_core.ui_task(run)
        reqs = []
        try:
            from beom_workbench.core import make, project
            conn, _ = project.load(settings, run["project_id"])
            req_snap = [{"id": str(r.get("id")), "text": str(r.get("text") or "")}
                        for r in make.requirements(conn, run["port_id"])] if run.get("port_id") else []
            mine = {k for k, m in _req_meta(run).items() if isinstance(m, dict) and m.get("from") == "user"}
            reqs = [(f"요구사항 {r['id']}", r["text"]) for r in req_snap if r["id"] not in mine]
        except Exception:  # noqa: BLE001 — 원장을 못 읽으면 계획 글만 본다
            pass
        meta = _req_meta(run)
        if req_snap and run.get("mode") == "request":
            # 단계와 요구사항을 잇는다 — 카드가 요구사항 밖 단계를 따로 보여 주고(요청에 없는 일이 섞였나), 빠진 요구사항이 없게.
            # '지금 있는 동작'(from=existing)은 맡는 단계가 없어도 된다 — 그대로 두는 동작이다
            ids = [r["id"] for r in req_snap]
            for s in clean:
                s["requirements"] = [str(x).strip() for x in s["requirements"] if str(x).strip()] if isinstance(s["requirements"], list) else []
            unknown = sorted({x for s in clean for x in s["requirements"] if x not in ids})
            uncovered = [i for i in ids if not any(i in s["requirements"] for s in clean) and (meta.get(i) or {}).get("from") != "existing"]
            if unknown or uncovered:
                problems.append("계획 단계와 요구사항이 맞물리지 않는다: " + "; ".join(
                    ([f"없는 요구사항 번호 {', '.join(unknown[:5])}"] if unknown else [])
                    + ([f"맡는 단계가 없는 요구사항 {', '.join(uncovered[:8])}"] if uncovered else []))
                    + " — 단계마다 그 단계가 이루는 요구사항을 steps.requirements 에 적는다 (준비·마무리 단계는 비워 둔다). "
                    "지금 요구사항: " + ", ".join(ids))
            missing = [i for i in ids if not meta.get(i)]
            if missing and any(meta.values()):
                problems.append("출처가 없는 요구사항이 있다: " + ", ".join(missing[:8]) + " — wb_make_requirements 로 요구사항마다 출처를 "
                                "적어 다시 저장한다. " + ORIGIN_HOW)
            plan_p = _origin_plan_problems(picks + carried, clean, meta, ids)
            if plan_p:
                problems.append("요구사항의 출처가 계획과 맞물리지 않는다: " + "; ".join(plan_p[:6]) + " — " + ORIGIN_PLAN_HOW)
            no_screen = _ui_without_screen({**run, "plan": {**prev, "steps": clean}})
            if no_screen:
                problems.append(no_screen + ". 사용자가 기준을 확인하기 전에 맞춘다 (확인한 뒤에 케이스를 더하면 다시 확인해야 한다)")
        # 화면 없는 제품(API·CLI)은 코드 이름이 곧 제품의 말이고, 요구사항의 파일 이름(설정 파일 등)도 명세다 — 번호만 본다.
        # 화면 작업인지는 이 계획이 화면·스타일 파일을 만들거나 고치는지(사실)로 본다
        ui = bool(_screen_files(clean))
        words = _user_texts(run)
        cases = [(f"합격 기준 '{str(c.get('name') or '')[:30]}'", str(c.get("description") or "")) for c in _golden_cases(run)] \
            if run.get("port_id") else []
        rule_fields = [(f"따를 규칙 '{str(r.get('rule') or '')[:20]}'", str(r.get("rule") or "")) for r in rules or []]
        planned = [f for st in clean for f in st["files"]]
        fresh = runner_core.new_project(run)
        jargon = (_card_jargon(run, fields + rule_fields, words, idents=ui, planned=planned, new_code=fresh)
                  + _card_jargon(run, why_fields, words, idents=False, planned=planned)
                  + _card_jargon(run, reqs + cases, words, paths=ui, idents=ui, planned=planned, new_code=fresh))
        if jargon:
            problems.append("카드에 보이는 글에 내부 표시가 있다: " + "; ".join(jargon[:8]) + " — 이 프로젝트의 파일 경로·코드 이름·요구사항 "
                    "번호는 카드 글에 쓰지 않는다. 단계가 맡는 요구사항은 steps.requirements 에, 고칠 파일은 steps.files 에, 따를 규칙이 "
                    "가리키는 코드 이름은 follows 의 code 에, 결정의 근거 파일은 decisions 의 basis 에 적고 (카드에 보이지 않는다), 제목·요약·"
                    "결정·요구사항은 화면에서 보이는 것과 동작의 말로 쓴다 (사용자가 먼저 쓴 말은 그대로 써도 된다). 합격 기준 설명은 "
                    ".beom/cases.json 을 고쳐 wb_port_capture 로 다시 저장한다")
    picks += carried
    _link_decisions(picks, _req_meta(run) if run is not None else {}, [r["id"] for r in req_snap])
    if layout_basis.strip() == "" and prev.get("layout_basis") and run is not None:
        layout_basis = prev["layout_basis"]  # 다시 낸 계획 — 앞에서 남긴 구조 근거를 이어 받는다
    shared, shared_error, not_shared = _shared_gate(run, clean, shared_changes, prev) if run is not None else ([], None, [])
    if shared_error:
        problems.append(shared_error)
    layout = _layout_gate(run, clean, layout_basis) if run is not None else []
    if layout:
        problems.append("새 프로젝트인데 정석 폴더 구조와 다른 곳이 있음: " + "; ".join(layout[:4]) + " — 그 언어의 정석 구조로 steps.files 를 "
                        "고친다 (설정·테스트 파일까지). 사용자가 구조를 정해 주어 다르게 하면 layout_basis 에 근거(요청의 어느 말)를 적는다")
    found = _research_items(research) or list((prev or {}).get("research") or [])  # 다시 낸 계획은 앞 조사를 이어 받는다
    if run is not None and (run.get("depth") == "deep" or found):
        miss = _research_problems(run, found)
        if miss:
            problems.append(miss)
    if run is not None and run.get("mode") in ("request", "reference"):
        lacking = _compare_missing(picks, prev)
        if lacking:
            problems.append("결정마다 사용자가 견주고 고를 수 있게 선택지별 좋은 점·아쉬운 점을 붙인다 — decisions 의 그 결정에 "
                            "compare=[{option: 선택지 이름 그대로, pros: 좋은 점, cons: 아쉬운 점, cost·effort: 해당하면}] 를 추천과 대안 모두에 "
                            "적고, why 에는 그중 추천하는 까닭을 쓴다. 빠진 것: " + "; ".join(lacking[:6]))
    if run is not None:
        cmp_src = [c["source"] for d in picks for c in d.get("compare") or [] if str(c.get("source") or "").startswith("http")]
        if cmp_src:
            opened = _opened_urls(run)
            hosts = {_host(u) for u in opened}
            unopened = [u for u in cmp_src if u not in opened and _host(u) not in hosts]
            if unopened:
                problems.append("decisions 의 compare 출처 중 이 실행에서 열지 않은 주소: " + ", ".join(unopened[:4])
                                + " — 직접 열어 본 페이지만 출처로 쓴다")
    if problems:
        return _refusal(problems, **refuse_extra)
    for d in picks:  # 깨진 시안 표시는 확인용 — 계획에는 남기지 않는다
        d.pop("sian_broken", None)
        d.pop("sian_tells", None)
        d.pop("looks_failed", None)

    def put(r):
        old = r.get("plan") or {}
        history = (old.get("history") or []) + ([{"steps": old["steps"], "summary": old.get("summary"),
                                                    **({"reqs": old["reqs"]} if old.get("reqs") else {}),
                                                    **({"decisions": old["decisions"]} if old.get("decisions") else {}),
                                                    "approved": bool(old.get("approved"))}]
                                                  if old.get("steps") else [])
        # 계획을 승인하며 결정을 바꾼 바로 그 라운드에 결정에 맞춰 다시 낸 계획은 사용자 결정의 결과다 — 다시 묻지 않는다
        keep = bool(old.get("approved")) and r.get("auto_approve_round") == r.get("round")
        mine = {d["topic"]: d["choice"] for d in old.get("decisions") or [] if d.get("changed_by_user")}
        for d in picks:  # 사용자가 직접 고른 결정을 그대로 실은 계획 — 출처(직접 고름)를 잃지 않는다
            if mine.get(d["topic"]) == d["choice"]:
                d["changed_by_user"] = True
        r["plan"] = {"steps": clean, "summary": summary.strip()[:1000], "approved": keep, "history": history[-5:],
                     **({"approved_via": "decision-change"} if keep else {}),
                     "at": inbox._now(), **({"design": design.strip()[:6000]} if design.strip() else {}),
                     **({"decisions": picks} if picks else {}), **({"look": lines} if lines else {}),
                     **({"research": found} if found else {}),
                     **({"reqs": req_snap} if req_snap else {}),
                     **({"previews": shots} if shots else {}), **({"follows": rules} if rules else {}),
                     **({"design_basis": design_basis.strip()[:300]} if design_basis.strip() else {}),
                     **({"layout_basis": layout_basis.strip()[:300]} if layout_basis.strip() else {}),
                     **({"shared_changes": shared} if shared else {}),
                     **({"design_scope": scope} if scope else {}),
                     **({"rules_checked": read} if read else {})}
    out = _update(put)
    if out.get("ok") and scope_note:
        out["design_scope"] = scope_note
    if out.get("ok") and conv is not None:
        out["conventions"] = conv  # 툴킷이 대신 뽑은 기존 화면의 규칙 — 계획은 받았다. 구현할 때 이것을 따른다
    if out.get("ok") and not_shared:
        out["shared_dropped"] = ("shared_changes 에서 뺐다 (다른 화면들이 쓰는 기존 공통 부품이 아니라 신고 없이 고친다): "
                                 + ", ".join(not_shared[:6]))
    early = _undo_code_before_plan(run, prev) if run is not None and out.get("ok") else []
    if out["ok"] and shared and run is not None:
        from beom_workbench.core import runner as runner_core
        now = inbox.load_run(settings, run["id"])
        waits_card = (now.get("plan_approval", True) and now.get("confirm_at", "start") != "end"
                      and not (now.get("plan") or {}).get("approved"))
        ask = [c for c in shared if c["file"] not in runner_core.shared_approved(now)]
        if ask and not waits_card:
            # 계획 카드가 없거나(끝에 한 번 확인·계획 승인 없음) 이미 승인이 이어진 계획 — 그 파일들을 사용자에게 따로 묻는다
            ans = inbox.ask(settings, run["id"], "permission", {
                "apply": {"kind": "shared", "files": [c["file"] for c in ask]},
                "summary": "다른 화면들이 쓰는 기존 공통 부품을 고치려 합니다 — " + "; ".join(
                    f"{c['file']}" + (f"({c['users']}곳)" if c.get("users") else "") + f": {c['why']}" for c in ask[:6]),
                "tool": "mcp:wb_plan_set", "input": {"shared_changes": ask}, "permission_kind": "shared"})
            files = [c["file"] for c in ask]
            if ans.get("allow"):
                inbox.update_run(settings, run["id"], lambda r: r.update(
                    shared_approved=sorted(set(r.get("shared_approved") or []) | set(files))))
            else:
                inbox.update_run(settings, run["id"], lambda r: r.update(
                    shared_refused=sorted(set(r.get("shared_refused") or []) | set(files))))
                out["shared_refused"] = files
                out["warning"] = ("사용자가 공통 부품 수정을 허락하지 않았다 (" + ", ".join(files) + ") — 그 파일은 고치지 말고 새 화면 쪽에서 "
                                  "해결하도록 계획을 고친다. 다시 신고하지 않는다")
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
    end = run is not None and run.get("confirm_at") == "end"
    return {**out, "note": "사용자 승인 전에는 코드를 바꾸지 않는다" if waits
            else "이 실행은 계획 승인을 기다리지 않는다 — 이어서 구현·검증한다 (사용자는 마지막에 확인한다)" if end
            else "이 실행은 계획 승인을 기다리지 않는다 — 요구사항·합격 기준을 사용자가 확인했으면 이어서 구현하고, 아직이면 next_step 대로 "
                 "확인을 기다린다"}


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
    out = _update(put)
    run = _run()
    steps = ((run or {}).get("plan") or {}).get("steps") or []
    if out.get("ok") and steps and all(s.get("status") in ("done", "skipped") for s in steps):
        left = before_handoff(run)  # 단계를 다 마쳤다 — 인계 때 볼 것을 지금 알린다
        out["before_handoff"] = left or ["없음 — wb_port_verify 로 검증한 뒤 인계한다"]
    return out


def _items(v, limit: int = 8, size: int = 300) -> list[str] | str:
    """목록으로 받은 항목은 한 줄씩 다듬고, 글 한 덩어리는 그대로 둔다 (예전 형식 호환)."""
    if isinstance(v, (list, tuple)):
        return [str(x).strip()[:size] for x in v if str(x).strip()][:limit]
    return str(v or "").strip()[:2000]


@mcp.tool()
@_record_refusal
def wb_handoff(summary: str, how_to_run: str | list[str], risks: str | list[str] = "", follow_ups: str | list[str] = "",
                changes: list[str] | None = None, checked: list[str] | None = None, choices: list[dict] | None = None) -> dict:
    """인수인계 — 완료 조건을 채운 뒤 남긴다. 사용자가 한눈에 보게 짧게, 항목은 목록으로:
    summary: 결과 한두 문장 (사용자 말로 — 무엇을 만들었고 지금 무엇을 할 수 있는지).
    changes: 사용자가 체감하는 변화 3~6개 (파일 이름이 아니라 기능으로).
    how_to_run: 실행 단계 목록 — 한 줄에 한 단계, 명령·경로는 `백틱`.
    checked: 툴킷 기록 밖에서 직접 해 본 확인만 (없으면 비운다) — 실제로 돌린 테스트·빌드·디자인 점검은 툴킷이 자기 기록으로 카드의
    '확인한 것'에 쓴다.
    risks: 남은 위험·확인 못 한 것, follow_ups: 이어서 할 일 — 각각 한 줄씩 최대 5개.
    내부 도구 이름·케이스 id·파일 목록 전체는 쓰지 않는다 (앱이 따로 보여 준다) — 계획과 다르게 한 파일(손대지 않은 계획 파일·계획에 없던
    파일)만 changes·risks 에 그 파일 이름과 까닭을 한 줄로 적는다 (끝난 뒤 사용자가 더 요청한 변경은 적지 않아도 된다).
    choices: 사용자가 단추로 고를 것 — 같은 사항에 방안이 여럿인 열린 질문 [{id: "Q2", question: "무엇을 정하는지 한 줄",
    context: "왜 지금 정해야 하는지·답에 따라 무엇이 달라지는지(어느 문서·화면·규칙)", options: [{name: "방안 이름(20자 안팎)",
    desc: "고르면 실제로 어떻게 되는지 한 줄 — 좋은 점·아쉬운 점"}, ...], recommended: "방안 이름", why: "추천 까닭 한 줄"}]
    (최대 8개, 방안 2~4개). 처음 보는 사람도 카드만 보고 고를 수 있어야 한다 — context·desc·why 가 없으면 받지 않는다.
    누르면 그 방안으로 정했다는 메시지가 와서 관련 문서·코드를 맞춰 고친다.
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
        chk = brief.check(run["target_root"], _policy_docs(run), run.get("brief_changes") if revising else None, _user_words(run))
        inbox.log(settings, run["id"], "brief_check", stage="handoff", **(chk.get("log") or {"ok": chk["ok"]}))
        if not chk["ok"]:
            return {"ok": False, "error": "기획 패키지가 아직 맞물리지 않음 — 고친 뒤 다시 인계한다", "problems": chk["problems"],
                    "findings": chk.get("findings") or []}
        broken = _wireframe_defects(run)
        if broken:  # 툴킷이 인계 직전에 목업을 직접 열어 본다 — 에이전트가 점검을 안 돌렸거나 결과를 넘겨도 결함 있는 그림이 나가지 않게
            inbox.log(settings, run["id"], "brief_check", stage="handoff_screens", ok=False, files=sorted(broken)[:10])
            return {"ok": False, "error": "목업에 화면 결함이 있음 — 고친 뒤 다시 인계한다 (wb_brief_screens 로 다시 확인)",
                    "problems": [f"{f}: {x}" for f, xs in broken.items() for x in xs[:4]][:20]}
    if run is not None and run.get("port_id") and run.get("project_id"):
        pending = _pending_approvals(run)
        if pending:  # 승인 뒤에 쓰면 '승인이 남았다'는 지난 내용이 인수인계에 남는다 — 무엇을 확인받는지 말하고 라운드를 끝낸다
            return {"ok": False, "error": f"사용자 확인이 {pending}건 남아 있음 — 인수인계는 승인 뒤에 쓴다. 무엇을 확인받는지 말하고 라운드를 끝낸다"}
        notice = _license_notice(run)
        if notice:  # 허용형 원본도 저작권 표기는 남겨야 한다 — 빠뜨린 채 인계하지 않게
            return {"ok": False, "error": "원본 저작권 표기가 대상에 없음 — 남긴 뒤 다시 인계한다", **notice}
    # 계획 때는 계획한 파일만 보았다 — 다 만든 실제 파일로 정석 구조·버전 고정 파일을 다시 본다
    problems = ["인계 전에 고칠 것: " + x for x in (_finish_checks(run) if run is not None else [])]
    if run is not None and run.get("mode") in ("request", "reference"):
        words = [summary, *_as_list(_items(how_to_run)), *_as_list(_items(risks, 6)), *_as_list(_items(follow_ups, 6)),
                 *(changes or []), *(checked or [])]
        # 확인했다는 주장은 한 일을 적는 칸(summary·changes·checked)에서만 본다 — risks 의 '비교하지 못했다'·how_to_run 의 '화면에서
        # 확인합니다'는 주장이 아니다 (리뷰)
        problems += _handoff_problems(run, words)
        problems += [x for x in (_design_unchecked(run, follow_ups), _ui_without_screen(run), _reqs_off_decisions(run), _reqs_off_plan(run),
                                 _review_missing(run, risks)) if x]
    problems += _handoff_choices(choices)[1]
    if problems:
        return _refusal(problems)
    data = {"summary": summary.strip()[:1500], "how_to_run": _items(how_to_run), "risks": _items(risks, 6),
            "follow_ups": _items(follow_ups, 6), "at": inbox._now()}
    if changes:
        data["changes"] = _items(changes, 8)
    if checked:
        data["checked"] = _items(checked, 6)
    facts = _verified(run) if run is not None and run.get("mode") in ("request", "reference") else []
    if facts:
        data["verified"] = facts
    rv = next((e for e in reversed(inbox.events(settings, run["id"])[0]) if e.get("kind") == "review"), None) if run is not None else None
    if rv and not rv.get("skipped"):  # 리뷰어가 말한 것 — 에이전트의 말과 따로 보여 준다
        data["review"] = {"findings": list(rv.get("findings") or [])[:8], "error": rv.get("error") or "", "langs": rv.get("langs") or []}
    last = _last_design_check(run) if run is not None and run.get("mode") in ("request", "reference") else None
    if last and (last.get("existing_defects") or last.get("new_issues") or last.get("layout_kept")):  # 툴킷이 잰 결과 — 에이전트의 말과 따로 보여 준다
        data["design_issues"] = {"existing": list(last.get("existing_defects") or [])[:12], "new": list(last.get("new_issues") or [])[:12],
                                 "layout": list(last.get("layout_kept") or [])[:12], "compared": list(last.get("compared") or [])[:4]}
    title = ""
    if run is not None and run.get("mode") == "plan" and run.get("auto_name"):
        from beom_workbench.core import brief as brief_mod
        try:
            head = next((ln[2:].strip() for ln in (brief_mod.folder(run["target_root"]) / "기획서.md").read_text(encoding="utf-8").splitlines()
                         if ln.startswith("# ")), "")
        except OSError:
            head = ""
        title = re.sub(r"\s*기획서$", "", head)[:40]

    def accept(r):
        r.update(handoff=data, follow_up_open=False)  # 추가 요청을 마무리했다
        if title:  # 사용자가 이름을 붙이지 않았다 — 요청 첫 문장 대신 기획서 제목으로
            r.update(name=title, auto_name=False)
        for s in (r.get("plan") or {}).get("steps") or []:  # 인계가 통과했다 — 진행 표시를 안 한 단계도 끝난 것으로 (건너뛴 것은 그대로)
            if s.get("status") in (None, "", "todo", "doing"):
                s["status"] = "done"
    picks = _handoff_choices(choices)[0]
    if picks:
        data["choices"] = picks
    out = _update(accept)
    if out["ok"]:
        inbox.log(settings, os.environ["BEOM_WORKBENCH_RUN"], "handoff")
    return out


def _code_diff(run: dict) -> tuple[str, list[str]]:
    """이번 실행에서 바꾼 제품 코드의 diff 와 파일 — 리뷰어가 읽는 것."""
    from beom_workbench.core import tasks
    from beom_workbench.core.checkpoints import Checkpoints
    base = tasks._start_checkpoint(settings, run)
    if not base:
        return "", []
    try:
        ch = Checkpoints(inbox.run_dir(settings, run["id"]), run["target_root"]).changes(base)
    except Exception:  # noqa: BLE001
        return "", []
    files = [f["path"] for f in ch.get("files") or [] if not _NOT_PRODUCT.search(f["path"]) and not f["path"].startswith(".beom/")
             and f["path"].lower().endswith(CODE_EXTS + (".jsp", ".sql", ".xml"))]
    keep = set(files)
    parts = re.split(r"(?m)^(?=diff --git )", ch.get("diff") or "")
    diff = "".join(x for x in parts if (m := re.match(r"diff --git a/(\S+) b/(\S+)", x)) and m.group(2) in keep)
    return diff, files


@mcp.tool()
@_record_refusal
def wb_review() -> dict:
    """독립 리뷰 — 이 대화를 모르는 리뷰어(같은 에이전트, 새 문맥, 읽기 전용)가 이번 실행에서 바꾼 코드의 diff 만 읽고, 바꾼 언어의
    점검표로 확실한 결함(확신 0.8 이상)만 알린다. 구현과 테스트를 마친 뒤, 인계 전에 부른다 (코드를 바꾼 작업은 인계 때 이 기록을 본다).
    findings: [{severity: high|medium|low, file, line, problem, fix, confidence}]. high 는 고치고 테스트를 다시 돌린다. 지적이 틀렸다고
    판단하면 고치지 않고 인계의 risks 에 그 파일과 까닭을 적는다 — 사용자가 인계 카드에서 지적과 함께 본다. 1~5분 걸린다."""
    from beom_workbench.core import review
    run = _run()
    if run is None:
        return {"ok": False, "error": "자동 실행 밖"}
    diff, files = _code_diff(run)
    if not diff.strip():
        inbox.log(settings, run["id"], "review", round=run.get("round"), files=[], findings=[], skipped="no_code")
        return {"ok": True, "findings": [], "note": "이번 실행에서 바꾼 제품 코드가 없어 리뷰할 것이 없음"}
    import hashlib
    from datetime import datetime
    sig = hashlib.sha1(diff.encode("utf-8", "replace")).hexdigest()[:16]
    past = [e for e in inbox.events(settings, run["id"])[0] if e.get("kind") == "review" and not e.get("skipped")]
    same = next((e for e in reversed(past) if e.get("sig") == sig), None)
    this_round = [e for e in past if e.get("round") == run.get("round")]
    if same or len(this_round) >= 2:
        # 리뷰는 한 라운드에 두 번까지, 같은 코드는 한 번 — 실험 149: 리뷰어가 드문 경우를 하나씩 더 찾아 7번을 부르며 코드가 부풀었다
        last = same or this_round[-1]
        return {"ok": True, "findings": last.get("findings") or [], "repeat": True,
                "note": ("이 코드는 이미 리뷰를 받았다" if same else "이번 라운드에 리뷰를 이미 두 번 받았다")
                        + " — 다시 돌리지 않는다. high 를 고쳤으면 테스트로 확인하고 인계한다 (남은 지적은 인계 카드에 보인다)"}
    res = review.run_review(settings, run, diff, files)
    if res.get("cost_usd"):  # 리뷰어 호출도 이 작업이 쓴 비용이다
        inbox.update_run(settings, run["id"], lambda r: r.update(cost_usd=round(float(r.get("cost_usd") or 0) + float(res["cost_usd"]), 4)))
    inbox.log(settings, run["id"], "review", round=run.get("round"), files=files[:40], findings=res.get("findings") or [], sig=sig,
              langs=res.get("langs") or [], **({"error": res["error"]} if res.get("error") else {}))
    if not res.get("ok"):
        return {"ok": True, "findings": [], "note": f"리뷰어를 돌리지 못함 ({res.get('error')}) — 리뷰 없이 이어 간다 (인계 카드에 그렇게 보인다)"}
    high = [f for f in res["findings"] if f["severity"] == "high"]
    return {"ok": True, "findings": res["findings"], "langs": res.get("langs"),
            "next": (f"high {len(high)}개는 고치고 테스트를 다시 돌린다 (지적이 틀렸으면 고치지 않고 인계 risks 에 파일과 까닭). " if high else "")
                    + ("medium·low 는 요청 범위 안에서 바로 고칠 수 있는 것만 고치고, 아니면 그대로 둔다 — 사용자가 인계 카드에서 본다. "
                       if len(res["findings"]) > len(high) else "")
                    + ("결함 지적 없음. " if not res["findings"] else "")
                    + "고친 뒤 리뷰를 다시 부르지 않는다 — 테스트로 확인하고 인계한다."}


def _review_missing(run: dict, risks=None, peek: bool = False) -> str | None:
    """코드를 바꾼 작업 — 독립 리뷰를 받았나, 그리고 high 지적을 고쳤거나 까닭을 남겼나. 리뷰어를 돌리지 못했으면(지원하지 않는
    에이전트·오류) 막지 않는다. 추가 요청 라운드는 그 라운드에서 바꾼 코드가 있으면 다시 받는다."""
    from datetime import datetime
    if run.get("mode") not in ("request", "reference") or (run.get("agent") or "claude") not in ("claude", "codex"):
        return None
    if run.get("review") is False or os.environ.get("BEOM_WORKBENCH_REVIEW") == "0":  # 실행 옵션·환경에서 끈 경우
        return None
    diff, files = _code_diff(run)
    if not files:
        return None
    changed = _last_change(run)
    reviews = [e for e in inbox.events(settings, run["id"])[0] if e.get("kind") == "review"]
    last = reviews[-1] if reviews else None
    try:
        at = datetime.fromisoformat(str((last or {}).get("at"))).timestamp()
    except ValueError:
        at = 0.0
    if last is None or (last.get("round") != run.get("round") and changed > at + 1):
        return ("코드를 바꿨는데 독립 리뷰를 받지 않았다 — 구현과 테스트를 마쳤으면 wb_review() 를 부른다 (이 대화를 모르는 리뷰어가 diff 만 "
                "읽고 확실한 결함만 알린다)")
    if last.get("error") or peek:
        return None
    high = [f for f in last.get("findings") or [] if f.get("severity") == "high"]
    if not high or changed > at + 1:
        return None  # 지적 뒤에 고쳤다 — 고친 것은 테스트·검증이 본다
    said = " ".join(str(x) for x in _as_list(_items(risks, 6))).lower()
    open_ = [f for f in high if Path(str(f.get("file") or "")).name.lower() not in said]
    if not open_:
        return None
    return ("독립 리뷰의 high 지적을 고치지 않았다: " + " / ".join(f"{f.get('file')}:{f.get('line')} {f.get('problem')}" for f in open_[:4])
            + " — 고치고 테스트를 다시 돌린다. 지적이 틀렸으면 인계의 risks 에 그 파일 이름과 까닭을 적는다")


def before_handoff(run: dict | None) -> list[str]:
    """인계 때 돌려보낼 것을 미리 — 인계 검사와 같은 함수로 계산한다 (따로 적은 목록이 아니라서 어긋나지 않는다). 인수인계 글로
    풀리는 것(계획과 다르게 한 파일의 까닭 등)은 '인계에 까닭을 적으면 된다'는 안내 그대로 보인다. 검증 결과·단계를 다 마쳤을 때·
    라운드 지시에 실어, 놓치거나 건너뛴 것을 인계 때에야 알게 되지 않게 한다."""
    if run is None or run.get("mode") not in ("request", "reference") or not (run.get("plan") or {}).get("steps"):
        return []
    out: list[str] = []
    for part in (lambda: _handoff_problems(run, []), lambda: [_design_unchecked(run, None, peek=True)],
                 lambda: [_ui_without_screen(run)], lambda: [_reqs_off_decisions(run)], lambda: [_reqs_off_plan(run)],
                 lambda: [_review_missing(run, None, peek=True)]):
        try:
            out += [x for x in part() if x]
        except Exception:  # noqa: BLE001 — 미리 알리기는 실패해도 막지 않는다 (인계 때 다시 본다)
            continue
    return list(dict.fromkeys(out))[:10]


def _reqs_off_plan(run: dict) -> str | None:
    """Before handing over — does a plan step take on every requirement? The plan is checked for this when it is submitted; a
    requirement added later (a follow-up) used to go straight to code with the old plan, so the user never saw a step for it
    (trial 145, Codex follow-up). Same rule as at plan time: 'existing behaviour' needs no step. Plans that never linked steps
    to requirements are left alone."""
    if run.get("mode") != "request" or not run.get("port_id") or not run.get("project_id"):
        return None
    steps = (run.get("plan") or {}).get("steps") or []
    if not any(s.get("requirements") for s in steps):
        return None
    try:
        from beom_workbench.core import make, project
        conn, _ = project.load(settings, run["project_id"])
        ids = [str(r.get("id")) for r in make.requirements(conn, run["port_id"])]
    except Exception:  # noqa: BLE001 — 읽지 못하면 막지 않는다
        return None
    meta = _req_meta(run)
    left = [i for i in ids if not any(i in (s.get("requirements") or []) for s in steps) and (meta.get(i) or {}).get("from") != "existing"]
    if not left:
        return None
    return ("계획 단계가 맡지 않는 요구사항이 있다: " + ", ".join(left[:8]) + " — 계획을 승인받은 뒤 더한 요구사항이다. 그것을 맡는 단계를 넣은 "
            "계획을 다시 낸다 (wb_plan_set, steps.requirements) — 사용자가 이번 일의 계획을 보고 확인한다")


def _reqs_off_decisions(run: dict) -> str | None:
    """넘기기 전 — 결정에 딸린 요구사항이 사용자가 고른 안의 것인가 (출처에 적힌 결정·안으로 견준다). 사용자가 승인하며 다른 안을
    골랐는데 요구사항을 맞추지 않은 채 끝내면 사용자가 고른 것이 빠진다."""
    if run.get("mode") != "request" or not run.get("port_id") or not run.get("project_id"):
        return None
    plan = run.get("plan") or {}
    picks = plan.get("decisions") or []
    meta = _req_meta(run)
    if not picks or not meta:
        return None
    try:
        from beom_workbench.core import make, project
        conn, _ = project.load(settings, run["project_id"])
        ids = [str(r.get("id")) for r in make.requirements(conn, run["port_id"])]
    except Exception:  # noqa: BLE001 — 읽지 못하면 막지 않는다
        return None
    clash = [x for x in _origin_plan_problems(picks, plan.get("steps") or [], meta, ids) if "decisions 에 없다" in x]
    # 사용자가 카드에서 안을 바꾼 결정 — 그 뒤에 그 결정에 딸린 요구사항을 다시 저장했나 (저장 시각으로 본다)
    saved = str(run.get("req_saved_at") or "")
    for topic, at in (run.get("decision_changes") or {}).items():
        tied = [k for k in ids if (meta.get(k) or {}).get("from") == "decision" and _squash(meta[k].get("decision")) == _squash(topic)]
        if tied and saved < str(at):
            clash.append(f"사용자가 '{topic}' 의 안을 바꾼 뒤 그 결정에 딸린 요구사항 {', '.join(tied[:6])} 을(를) 다시 저장하지 않았다")
    if not clash:
        return None
    return ("요구사항이 계획의 결정(사용자가 고른 안)과 어긋난 채 남아 있다: " + "; ".join(clash[:6]) + " — 요구사항·합격 케이스·구현을 고른 안에 "
            "맞추고(wb_make_requirements → wb_port_capture → wb_port_verify) 인계한다")


def _verified(run: dict) -> list[str]:
    """툴킷이 자기 기록으로 쓰는 '확인한 것' — 에이전트의 말이 아니라 실제로 돌린 테스트·빌드와 디자인 점검. 인수인계 카드에 따로 보인다
    (실험: 에이전트가 '사용자 확인을 마쳤다'를 확인한 근거처럼 썼다 — 확인한 일은 기록이 말한다)."""
    from datetime import datetime
    from beom_workbench.app.view import _inner_command
    evs = inbox.events(settings, run["id"])[0]
    changed = _last_change(run)
    out = []
    for task, label in (("test", "프로젝트 테스트"), ("build", "빌드"), ("typecheck", "타입 검사"), ("lint", "린트"), ("format", "형식 검사")):
        last = next((e for e in reversed(evs) if e.get("kind") == "task" and e.get("task") == task), None)
        try:
            at = datetime.fromisoformat(str((last or {}).get("at"))).timestamp()
        except ValueError:
            at = 0.0
        if last and last.get("exit_code") == 0 and at + 1 >= changed:  # 마지막으로 고친 뒤에 돈 것만 — 그 전 결과는 지금 코드의 확인이 아니다
            cmd = _inner_command(str(last.get("command") or ""))
            cmd = cmd if len(cmd) <= 160 else cmd[:157].rsplit(" ", 1)[0] + " …"  # 자르면 낱말 경계에서 (실험: 80자에서 잘려 읽지 못했다)
            out.append(f"{label} `{cmd}` 통과" if cmd else f"{label} 통과")
    chk = _last_design_check(run)
    if chk:
        pages = chk.get("pages") or []
        n = len({e.get("sig") or (e.get("page"), e.get("state")) for e in pages if not str(e.get("size") or "").startswith("모바일")}) or len(pages)
        sizes = "PC·휴대폰" if any(str(e.get("size") or "").startswith("모바일") for e in pages) else "PC"
        out.append(f"마지막 수정 뒤 화면 {n}곳을 {sizes} 크기로 찍어 점검 — 고칠 것 {int(chk.get('fix_count') or 0)}개"
                   + (f", 기존 화면 {len(chk['compared'])}곳과 함께 재어 견줌" if chk.get("compared") else "")
                   + (f", AI 티로 읽히는 것 {len(chk['ai_tells'])}개 남김" if chk.get("ai_tells") else ""))
    return out


def _last_change(run: dict) -> float:
    """시작 뒤 바뀐 제품 파일 중 가장 늦게 고친 시각 (모르면 0)."""
    from beom_workbench.core import tasks
    from beom_workbench.core.checkpoints import Checkpoints
    base = tasks._start_checkpoint(settings, run)
    if not base:
        return 0.0
    try:
        files = [f["path"] for f in Checkpoints(inbox.run_dir(settings, run["id"]), run["target_root"]).changes(base).get("files") or []]
    except Exception:  # noqa: BLE001
        return 0.0
    root = Path(run["target_root"])
    stamp = 0.0
    for f in files:
        if _NOT_PRODUCT.search(f) or f.startswith(".beom/"):
            continue
        try:
            stamp = max(stamp, (root / f).stat().st_mtime)
        except OSError:
            continue
    return stamp


def _refusal(problems: list[str], **extra) -> dict:
    """돌려보낼 때는 걸린 것을 모두 한 번에 — 첫 문제만 알리면 에이전트가 그것만 고쳐 다시 내고 다음 문제에 또 걸려, 계획·인계를
    두세 번씩 다시 냈다 (실험: 계획 거절 8번 중 6번이 앞의 것을 고친 바로 다음 제출에서 다른 검사에 걸렸다)."""
    seen: list[str] = []
    for p in problems:
        if p and p not in seen:
            seen.append(p)
    if len(seen) == 1:
        return {"ok": False, "error": seen[0], **extra}
    return {"ok": False, "error": f"고칠 것 {len(seen)}가지 — 모두 고친 뒤 한 번에 다시 낸다: "
                                  + " ".join(f"({i}) {p}" for i, p in enumerate(seen, 1)),
            "problems": seen, **extra}


def _as_list(x) -> list:
    """목록이면 그대로, 글 하나면 한 줄짜리 목록 — 글을 * 로 풀면 글자 하나하나가 되어 검사가 빗나갔다."""
    return list(x) if isinstance(x, (list, tuple)) else ([x] if x else [])


# 인수인계에 앱 밖에서는 뜻이 없는 표시 — 도구 이름·툴킷 작업 폴더. 케이스 id 와 요구사항 번호는 실제 이름과 견준다 (_handoff_tokens).
# 낱말 목록('합격 케이스'·'라운드'·'툴킷'·'사용자 확인을 마쳤다' …)으로 돌려보내지 않는다 — 앱 화면이 그 말을 그대로 쓰고, 목록은
# '관리자 승인 카드' 같은 말을 잘못 읽었다 (실험: 인계 거절의 절반이 이 목록). 확인한 일은 툴킷이 자기 기록으로 카드에 따로 쓴다 (verified).
_HANDOFF_TOOLS = re.compile(r"\bmcp__[\w-]+|\bwb_(?:port|plan|handoff|design|screen|task|deps|ui_conventions|activity|person_ask|brief|"
                            r"action|run|remove|make)\w*|(?<![\w.])\.beom\b")  # 툴킷 도구 이름 — 'wb_member 테이블' 같은 제품 이름은 아니다


def _handoff_tokens(run: dict, words: list[str]) -> list[str]:
    """인수인계 글에 든 도구 이름·.beom·이 실행의 케이스 id(이름 그대로) — 글을 읽지 않고 이름을 견준다."""
    text = "\n".join(str(w) for w in words)
    hits = [m.group(0) for m in _HANDOFF_TOOLS.finditer(text)]
    names = {str(c.get("name")) for c in _golden_cases(run) if run.get("port_id") and "_" in str(c.get("name") or "")}
    hits += [n for n in names if re.search(rf"(?<![\w]){re.escape(n)}(?![\w])", text)]
    return list(dict.fromkeys(hits))


# 확인했다는 주장 — 툴킷 기록에 그 근거가 있어야 한다 (실험: 기존 화면과 비교한 적 없이 '기존과 비교해 모두 같았다',
# HTTP 로만 확인하고 '화면에서 눌러 보고 확인')
STYLE_ONLY_EXTS = (".css", ".scss", ".sass", ".less", ".html", ".htm", ".j2", ".jinja", ".jinja2", ".njk", ".hbs")


def _unlinked_changes(run: dict, said: str) -> list[str]:
    """마지막 검증이 잰 '바꾼 기존 함수' 중 어떤 요구사항(매핑한 구현 위치)에도 연결되지 않았고 인계에 이름이 없는 것 — 실험 142: 주문 취소를
    만들며 기존 '다음 단계로'(advance)와 repo.update_status 의 동작·인자까지 바꿨는데 인계에 없어, 사용자는 기존 기능이 바뀐 것을 몰랐다."""
    from beom_workbench.core import project
    if not run.get("project_id") or not run.get("port_id"):
        return []
    try:
        conn, _ = project.load(settings, run["project_id"])
        row = conn.execute("SELECT detail FROM port_check WHERE port_id = ? AND kind = 'impact' ORDER BY rowid DESC LIMIT 1",
                           (run["port_id"],)).fetchone()
        refs = [r["target_ref"] for r in conn.execute("SELECT target_ref FROM port_map WHERE port_id = ? AND status = 'ported'",
                                                       (run["port_id"],))]
    except Exception:  # noqa: BLE001 — 기록을 못 읽으면 인계를 막지 않는다
        return []
    if not row:
        return []
    d = json.loads(row["detail"]) if isinstance(row["detail"], str) else row["detail"] or {}
    mapped = []
    for ref in refs:
        m = re.fullmatch(r"(.+?):(\d+)", str(ref or ""))
        if m:
            mapped.append((m.group(1).replace("\\", "/"), int(m.group(2))))
    low = said.lower()
    out = []
    for s in d.get("changed_spans") or []:
        path, _, start = str(s.get("at") or "").rpartition(":")
        if not path or not start.isdigit():
            continue
        lo, hi = int(start), int(s.get("end") or start)
        if s.get("existed") is not True:
            continue  # 이번에 새로 만든 함수(기존 파일 안이라도)이거나 시작 전 모습을 몰라 가리지 못한 것
        if any(p == path and lo <= ln <= hi for p, ln in mapped):
            continue  # 요구사항을 이루려고 바꾼 함수
        name = str(s.get("name") or "")
        if name and re.search(r"(?<![\w])" + re.escape(name.lower()) + r"(?![\w])", low):
            continue  # 인계에 밝혔다 (낱말로 — 'get' 같은 짧은 이름이 다른 낱말 안에 있다고 밝힌 것이 아니다)
        out.append(f"{s['at']} {name}")
    return out


def _open_screen_rules(run: dict, said: str) -> list[str]:
    """마지막 검증의 기존 화면 규칙 결과 중 고치지도, 인계에 까닭을 밝히지도 않은 곳 — 실험: 중복 값 목록·새 번역 키를 사용자 카드로만
    올려 모의 사용자가 매번 잡았다. 에이전트가 먼저 고치거나 밝힌다."""
    from beom_workbench.core import project
    if not run.get("project_id") or not run.get("port_id"):
        return []
    try:
        conn, _ = project.load(settings, run["project_id"])
        row = conn.execute("SELECT detail FROM port_check WHERE port_id = ? AND kind = 'screen_rules' ORDER BY rowid DESC LIMIT 1",
                           (run["port_id"],)).fetchone()
    except Exception:  # noqa: BLE001
        return []
    if not row:
        return []
    d = json.loads(row["detail"]) if isinstance(row["detail"], str) else row["detail"] or {}
    low = said.lower()
    out = []
    for f in d.get("findings") or []:
        at = str(f.get("at") or "").rsplit(":", 1)[0]
        if not at or at == "." or "실패" in str(f.get("summary") or "")[:20] or f.get("kept"):
            continue
        name = Path(at).name.lower()
        if name and name not in low and Path(at).stem.lower() not in low:
            out.append(f"{f.get('at')} {str(f.get('summary') or '')[:60]}")
    return out


CODE_EXTS = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".go", ".rs", ".java", ".kt", ".cs", ".css", ".scss",
             ".html")
CHECKS = (("lint", "린트"), ("format", "형식 검사"), ("typecheck", "타입 검사"))


def _checks_not_run(run: dict, root: Path, product: list[str]) -> list[str]:
    """프로젝트가 갖춘 린트·형식 검사·타입 검사 — 있으면 마지막 수정 뒤에 돌았고, 이번에 바꾼 파일에서 실패하지 않아야 넘긴다.
    프로젝트가 그 도구를 갖추지 않았으면 요구하지 않는다 (툴킷이 규칙을 정하지 않는다). 바꾸지 않은 파일의 실패는 원래 있던 것이라 막지 않고,
    바꾼 파일의 실패도 처음 한 번만 돌려보낸다 (원래 그 파일에 있던 문제일 수 있다 — 남기면 risks 에 적는다)."""
    from datetime import datetime
    from beom_workbench.core import tasks
    code = [f for f in product if f.lower().endswith(CODE_EXTS)]
    if not code:
        return []
    stamp = 0.0
    for f in code:
        try:
            stamp = max(stamp, (root / f).stat().st_mtime)
        except OSError:
            continue
    evs = [e for e in inbox.events(settings, run["id"])[0] if e.get("kind") == "task"]
    out = []
    for task, label in CHECKS:
        try:
            if not tasks.available(root, task):
                continue
        except Exception:  # noqa: BLE001 — 고를 수 없으면 요구하지 않는다
            continue
        runs = [e for e in evs if e.get("task") == task]
        try:
            at = datetime.fromisoformat(str(runs[-1].get("at"))).timestamp() if runs else 0.0
        except ValueError:
            at = 0.0
        if not runs or at + 1 < stamp:
            out.append(f"이 프로젝트에는 {label}가 갖춰져 있는데 마지막으로 고친 뒤에 돌리지 않았다 — wb_task(task=\"{task}\") 로 돌린다 "
                       "(린트·형식 검사는 툴킷이 이번에 바꾼 파일만 본다). 이번에 바꾼 파일에서 나온 것은 고친다")
            continue
        last = runs[-1]
        if last.get("exit_code") in (0, None):
            continue
        tail = str(last.get("tail") or "").replace("\\", "/")
        hit = [f for f in code if f in tail or (len(Path(f).name) > 4 and Path(f).name in tail)]
        if not hit:
            continue  # 바꾸지 않은 파일에서만 실패 — 원래 있던 문제다
        import hashlib
        sig = hashlib.sha1((task + "|" + "|".join(sorted(hit))).encode()).hexdigest()[:12]
        if (run.get("check_block_sig") or {}).get(task) == sig:
            continue
        inbox.update_run(settings, run["id"], lambda r: r.setdefault("check_block_sig", {}).update({task: sig}))
        out.append(f"{label}가 이번에 바꾼 파일에서 실패한다 ({', '.join(hit[:5])}) — 고치고 wb_task(task=\"{task}\") 로 다시 돌린다. "
                   "고친 줄이 아니라 원래 그 파일에 있던 문제만 남았으면 인계의 risks 에 그렇다고 적고 다시 인계한다")
    return out


def _tests_not_run(run: dict, root: Path, product: list[str]) -> str | None:
    """마지막 수정 뒤에 프로젝트 테스트가 통과했나 — 실험: 테스트 실행이 결과 없이 끝났는데 테스트를 돌리지 않은 채 인계했다."""
    from datetime import datetime
    stamp = 0.0
    for f in product:
        try:
            stamp = max(stamp, (root / f).stat().st_mtime)
        except OSError:
            continue
    runs = [e for e in inbox.events(settings, run["id"])[0] if e.get("kind") == "task" and e.get("task") == "test"
            and not e.get("regression")]  # 회귀 확인은 기록된 표시로 가른다 (명령 글로 가르지 않는다 — 툴킷도 .beom/venv 로 돌린다)
    if not runs:
        return "프로젝트 테스트를 돌린 기록이 없다 — wb_task(task=\"test\") 로 돌려 통과를 확인한 뒤 인계한다"
    last = runs[-1]
    try:
        at = datetime.fromisoformat(str(last.get("at"))).timestamp()
    except ValueError:
        return None
    if at + 1 < stamp:
        return "마지막으로 고친 뒤에 프로젝트 테스트를 다시 돌리지 않았다 — wb_task(task=\"test\") 로 다시 돌려 통과를 확인한 뒤 인계한다"
    code = last.get("exit_code")
    if isinstance(code, int) and code != 0:
        return f"마지막 프로젝트 테스트가 실패했다 (종료 코드 {code}) — 고쳐서 통과시킨 뒤 인계한다"
    return None
_TEST_FILE = re.compile(r"(?:^|/)(?:tests?|__tests__|spec)/|(?:^|/)test_[^/]+\.py$|_test\.(?:py|go)$|\.(?:test|spec)\.[cm]?[jt]sx?$|"
                        r"(?:Tests?|Spec)\.(?:java|kt|cs|scala)$|(?:^|/)src/test/", re.I)
_NOT_PRODUCT = re.compile(r"(?:^|/)(?:package-lock\.json|pnpm-lock\.yaml|yarn\.lock|bun\.lockb?|poetry\.lock|uv\.lock|Cargo\.lock|"
                          r"go\.sum|Pipfile\.lock|packages\.lock\.json)$|^\.gitignore$")  # .gitignore — 툴킷이 고치기도 한다


def _handoff_problems(run: dict, words: list[str]) -> list[str]:
    """인수인계를 받기 전에 툴킷이 보는 것 — 다른 도구들처럼 최종 변경을 요청·계획과 대조한다.
    1) 내부 말이 없나. 2) 테스트 러너가 있는 프로젝트면 이번 변경에 프로젝트 테스트가 있나 (합격 케이스는 커밋되지 않는 .beom 에만
    있어, 커밋된 테스트가 핵심 기능을 하나도 확인하지 않은 채 넘어갔다). 3) 계획에 없던 파일을 바꿨으면 인계에 밝혔나.
    걸린 것을 모두 돌려준다 — 하나씩 돌려보내면 고쳐 다시 낼 때마다 다음 것에 걸려 인계를 서너 번 냈다 (실험)."""
    out: list[str] = []
    bad = _handoff_tokens(run, words)
    if bad:
        out.append("인수인계에 앱 밖에서는 뜻이 없는 이름이 있다 (" + ", ".join(bad[:5]) + ") — 도구 이름·케이스 id 대신 그 기능과 확인한 일을 "
                "말로 쓴다")
    rids = _card_jargon(run, [("인수인계", w) for w in words], paths=False, idents=False)
    if rids:  # 인수인계 카드에는 요구사항 목록이 없다 — 번호만 적으면 무엇인지 모른다
        out.append("인수인계에 요구사항 번호가 있다 (" + "; ".join(rids[:3]) + ") — 번호 대신 그 기능을 말로 쓴다")
    from beom_workbench.core import tasks
    from beom_workbench.core.checkpoints import Checkpoints
    base = tasks._start_checkpoint(settings, run)
    if not base:
        return out
    try:
        files = [f["path"] for f in Checkpoints(inbox.run_dir(settings, run["id"]), run["target_root"]).changes(base).get("files") or []]
    except Exception:  # noqa: BLE001 — 비교를 못 하면 인계를 막지 않는다
        return out
    root = Path(run["target_root"])
    product = [f for f in files if not _NOT_PRODUCT.search(f)]
    tests = [f for f in product if _TEST_FILE.search(f)]
    try:
        has_runner = tasks.available(root, "test")
    except Exception:  # noqa: BLE001
        has_runner = False
    # 느낌만·전체 디자인을 바꾸며 스타일·마크업만 고쳤으면 동작이 그대로라 새 테스트를 요구하지 않는다
    # (실험: 요구하니 'CSS 에 --bg 가 있다' 같은 글자 확인 테스트를 만들어 채웠다)
    from beom_workbench.core import runner as runner_core
    style_only = bool(product) and all(f.lower().endswith(STYLE_ONLY_EXTS) for f in product) and \
        runner_core.task_profile(run).get("design") in ("restyle", "redesign")
    if product and has_runner and not tests and not style_only:
        out.append("이번 변경을 확인하는 프로젝트 테스트가 없다 — 요구사항마다 프로젝트의 테스트 방식으로 테스트를 남긴다 (합격 케이스는 "
                "커밋되지 않아 나중에 다시 돌릴 수 없다). wb_task(task=\"test\") 로 통과를 확인한 뒤 인계한다")
    elif product and has_runner and not style_only:
        stale = _tests_not_run(run, root, product)
        if stale:
            out.append(stale)
    out += _checks_not_run(run, root, product)
    said_all = " ".join(str(w) for w in words)
    open_rules = _open_screen_rules(run, said_all)
    if open_rules:
        out.append("기존 코드와 다르게 만들거나 이미 있는 것을 다시 만든 곳이 남아 있다: " + "; ".join(open_rules[:5]) + " — 근거의 기존 것을 그 자리에서 "
                "불러 쓰게 고치고(기존 파일을 옮기거나 다른 기능의 파일을 고치지 않는다) wb_port_verify 로 다시 확인한다. 일부러 그렇게 둔 곳이면 wb_port_verify(kept=[{at: 파일, why: 까닭}]) 로 까닭을 남기거나 인계의 risks 에 그 파일과 까닭을 적는다 (사용자가 승인 카드에서 본다)")
    unlinked = _unlinked_changes(run, said_all)
    if unlinked:
        out.append("요구사항에 연결되지 않은 기존 함수를 바꿨다: " + "; ".join(unlinked[:5]) + " — 요청에 필요 없는 변경이면 되돌린다. 필요했으면 "
                   "인계의 risks 에 그 함수 이름과, 원래 하던 동작에서 무엇이 달라지는지 쓴다 (사용자는 기존 기능이 바뀐 것을 알아야 한다)")
    plan_steps = (run.get("plan") or {}).get("steps") or []
    planned = {_rel(f) for s in plan_steps for f in s.get("files") or []}
    if planned:
        said = " ".join(str(w) for w in words).lower()
        # 계획한 파일을 손대지 않았으면 — 요청한 변경을 빠뜨렸거나 되돌린 것 (실험: 메뉴 링크를 넣었다가 디자인 점검의 비교 대상으로 쓰려고
        # 지웠고, 계획 단계는 '끝남'으로 남았다)
        skipped = {_rel(f) for s in plan_steps if s.get("status") == "skipped" for f in s.get("files") or []}
        untouched = [f for f in sorted(planned) if f not in files and f not in skipped and not f.endswith("/")
                     and not f.startswith(".beom/") and not (root / f).is_dir()
                     and not any(ch in f for ch in "*?") and Path(f).name.lower() not in said and Path(f).stem.lower() not in said]
        if untouched:
            out.append("계획한 파일을 바꾸지 않았다: " + ", ".join(untouched[:6]) + " — 계획대로 고치거나, 하지 않은 까닭을 인계의 risks 에 적는다 "
                    "(그 단계는 wb_plan_progress 로 skipped). 툴킷 점검(디자인 비교 등)을 맞추려고 요청한 변경을 되돌리지 않는다")
        plan_at = str((run.get("plan") or {}).get("at") or "")
        asked_more = any(str(m.get("at") or "") > plan_at for m in run.get("messages") or [] if isinstance(m, dict) and m.get("text"))
        extra = [] if asked_more else [f for f in product if f not in planned and f not in tests and Path(f).name.lower() not in said
                                         and Path(f).stem.lower() not in said]
        if extra:
            out.append("계획에 없던 파일을 바꿨다: " + ", ".join(extra[:6]) + " — 요청에 필요한 변경이 아니면 되돌린다 (README·빌드 설정·스타일을 "
                    "요청 없이 다시 쓰지 않는다). 필요했으면 인계의 changes 나 risks 에 무엇을 왜 바꿨는지 적는다")
    return out


def _ui_changes(run: dict) -> tuple[list[str], str | None]:
    """시작 전 대비 바뀐 화면 파일(화면 템플릿·컴포넌트·스타일)과 그 내용의 지문 — 디자인 점검이 '마지막 수정 뒤'였는지 견준다."""
    import hashlib
    from beom_workbench.core import conventions, tasks
    from beom_workbench.core.checkpoints import Checkpoints
    exts = conventions.SCREEN_EXTS + (".css", ".scss", ".sass", ".less")
    base = tasks._start_checkpoint(settings, run)
    if not base:
        return [], None
    try:
        files = [f["path"] for f in Checkpoints(inbox.run_dir(settings, run["id"]), run["target_root"]).changes(base).get("files") or []]
    except Exception:  # noqa: BLE001 — 비교를 못 하면 막지 않는다
        return [], None
    root = Path(run["target_root"])
    ui = sorted(f for f in files if f.lower().endswith(exts) and not f.startswith(".beom/") and not _TEST_FILE.search(f))
    if not ui:
        return [], None
    h = hashlib.sha256()
    for f in ui:
        try:
            h.update(f.encode() + b"\0" + (root / f).read_bytes() + b"\0")
        except OSError:
            h.update(f.encode() + b"\0-\0")  # 지운 파일
    return ui, h.hexdigest()[:16]


# 외부 검출기의 규칙 중 우리 점검기가 같은 것을 재는 것 — 우리 쪽이 이미 잡았으면 겹쳐 싣지 않는다 (렌더한 화면으로 잰 쪽이 더 정확하다)
DETECTOR_SAME = {"gradient-text": "gradient_text", "kicker-above-heading": "eyebrow", "hero-eyebrow-chip": "eyebrow",
                 "nested-cards": "nested_cards", "bounce-easing": "bounce", "hard-offset-shadow": "hard_shadow", "dark-glow": "glow",
                 "side-tab": "side_stripe", "border-accent-on-rounded": "side_stripe", "icon-tile-stack": "card_grid"}


def _detector_tells(run: dict, root: Path, ours: list[str]) -> list[str]:
    """외부 디자인 검출기(관리되는 오픈소스 규칙 모음, 툴킷이 설치해 돌린다)로 바뀐 화면 파일을 정적으로 검사해, 우리 점검기가 재지 못하는
    'AI 티'(팔레트·장식 줄무늬·CSS 의 한쪽 띠·상투적 문구 등)만 보탠다. 설치·실행이 안 되면 빈 목록 — 점검은 우리 규칙으로 이어 간다.
    검증 (14개 화면, 사람 판정): 우리 규칙과 잡는 것이 서로 달라 합치면 놓치는 것이 줄었다 — 단독으로 쓰기에는 어느 쪽도 모자랐다."""
    if os.environ.get("BEOM_WORKBENCH_DESIGN_DETECTOR") == "0":
        return []
    from beom_workbench.core import designlint
    ui, _ = _ui_changes(run)
    files = [str(root / f) for f in ui if (root / f).is_file() and f.lower().endswith((".html", ".htm", ".css", ".scss", ".jsx", ".tsx",
                                                                                         ".vue", ".svelte", ".js"))][:40]
    if not files:
        return []
    res = designlint.scan(settings, files)
    if not res.get("ok"):
        inbox.log(settings, run["id"], "note", tone="", text=f"외부 디자인 검출기를 쓰지 못해 툴킷 규칙으로만 점검했습니다 ({res.get('error')})")
        return []
    have = {k for k, label in TELLS.items() if any(label in t for t in ours)}
    out, seen = [], set()
    for f in res["findings"]:
        if f["category"] != designlint.SLOP or f["rule"] in seen or DETECTOR_SAME.get(f["rule"]) in have:
            continue
        seen.add(f["rule"])
        where = Path(f["where"].split(":")[0]).name + (":" + f["where"].rsplit(":", 1)[-1] if ":" in f["where"] else "")
        out.append(f"AI 티 — {f['name']} (외부 검출기: {f['snippet'] or where})" + (f" — {f['description'][:120]}" if f["description"] else ""))
    return out[:6]


def _design_unchecked(run: dict, follow_ups, peek: bool = False) -> str | None:
    """화면을 바꿨으면 마지막 수정 뒤에 디자인 점검을 했어야 인계한다 — 기존 제품도. 규칙으로만 시켰더니 Claude 실행 5개 중 5개가
    점검 없이 'PC·모바일 화면 확인'이라고 인계했고, 그중 둘은 모바일에서 가로로 넘쳤다 (다른 도구들도 화면 변경은 스크린샷으로 확인한 뒤
    끝낸다). 무엇이 기존 제품의 문제인지는 에이전트의 말이 아니라 기존 화면을 함께 잰 결과(compare)로 가른다 — 새 화면에서만 나온
    문제는 고치게 하고, 그래도 남기면 사용자가 인계에서 그대로 본다."""
    import hashlib
    from beom_workbench.core import screens
    if run.get("mode") not in ("request", "reference") or not screens.available():
        return None
    ui, fp = _ui_changes(run)
    if not fp:
        return None
    from beom_workbench.core import runner as runner_core
    had_screens = runner_core.screens_at_start(run)  # 견줄 기존 화면이 있었나 (기존 코드가 있다는 것과 다르다)
    last = _last_design_check(run, fp)
    if last is None:
        return ("화면 파일을 바꿨는데 마지막 수정 뒤 디자인 점검을 하지 않았다 (" + ", ".join(ui[:4]) + ") — "
                "wb_design_check() 를 인자 없이 부르면 툴킷이 화면 케이스와 같은 경로·동작·서버로 바뀐 화면을 열고 견줄 기존 화면까지 골라 함께 잰다 (화면 케이스로 열 수 없는 화면만 files·url·paths 로 직접 준다). 새 화면에서 오는 fix 는 고치고 다시 "
                "점검한 뒤 인계한다")
    scope = design_scope(run)
    if had_screens and scope in ("restyle", "redesign"):
        # 디자인을 바꾸는 작업은 공통 스타일이 모든 화면에 걸린다 — 한 화면만 보고 '점검 통과'라고 했다 (실험: 목록 하나만 점검)
        seen, need = _design_coverage(run, fp)
        if len(seen) < need:
            # 마지막 수정 뒤의 점검만 센다 — 그것을 말하지 않으니 고친 뒤 한 곳만 다시 보고 또 인계해 두 번 돌아왔다 (실험)
            return (f"디자인을 바꾸는 작업인데 마지막으로 화면 파일을 고친 뒤 점검한 화면이 {len(seen)}곳뿐이다 — 고친 뒤에는 앞의 점검이 "
                    f"지난 것이 된다. 바뀐 공통 스타일이 걸리는 서로 다른 화면 {need}곳 이상을 한 번에 다시 점검한 뒤 더 고치지 않고 인계한다 — "
                    "wb_design_check() 를 인자 없이 부르면 툴킷이 화면 케이스와 같은 경로·동작·서버로 바뀐 화면을 열고 견줄 기존 화면까지 골라 함께 잰다. 그래도 모자라면 눌러 열어야 보이는 화면(메뉴·상세·팝업)을 paths·files 에 actions 와 함께 더한다. 같은 화면은 "
                    "주소를 바꿔도 한 곳으로 센다")
    keep_design = had_screens and scope not in ("restyle", "redesign")
    tried = any((c.get("result") or {}).get("compare_refused") for c in _design_checks(run))
    if keep_design and last.get("fix_count") and not last.get("compared") and not tried:
        return ("마지막 디자인 점검에 고칠 것이 " + str(last["fix_count"]) + "개 있는데 기존 화면과 견주지 않았다 — wb_design_check() 를 인자 없이 부르면 툴킷이 화면 케이스와 같은 경로·동작·서버로 바뀐 화면을 열고 견줄 기존 화면까지 골라 함께 잰다. 툴킷이 기존 화면에도 있는 문제(기존 제품의 고칠거리로 사용자에게 알린다)와 새 화면에서만 "
                "나온 문제(고친다)를 가른다 — '기존 문제'라는 말만으로는 넘기지 않는다. 기존 화면을 띄울 수 없으면(서버·로그인 필요) compare 에 "
                "넣어 본 결과 툴킷이 그렇다고 남기고, 그때는 남은 것을 follow_ups 에 적어 인계한다")
    new = last.get("new_issues") if "new_issues" in last else ([f"고칠 것 {last['fix_count']}개"] if last.get("fix_count") else [])
    tells = last.get("ai_tells") or []
    if not new and tells and not peek:
        # AI 티는 취향이라 막지 않는다 — 처음 보는 것만 한 번 돌려보내 바꿀 기회를 준다 (규칙에 적어 두기만 했더니 승인한 시안의 제목 위 라벨·한쪽 색 띠가
        # 그대로 나갔다 — 실험 147). 다시 인계하면 받는다; 남은 것은 인계 카드에 보인다
        tsig = hashlib.sha1(json.dumps(sorted(tells), ensure_ascii=False).encode()).hexdigest()[:12]
        if run.get("design_tell_sig") != tsig:
            inbox.update_run(settings, run["id"], lambda r: r.update(design_tell_sig=tsig))
            return (f"마지막 디자인 점검에 AI 티로 읽히는 것이 {len(tells)}개 있다: " + " / ".join(t[5:90] for t in tells[:5]) + " — 적힌 대로 바꾸고 "
                    "다시 점검한 뒤 인계한다. 분야에 꼭 맞아 일부러 쓴 것이면 그대로 다시 인계해도 된다 (사용자가 인계 카드에서 본다)")
    if not new:
        return None
    if peek:  # 미리 알릴 때 — 인계 때의 '한 번 돌려보내기' 기록은 건드리지 않는다
        return (f"새 화면에서만 나온 디자인 문제 {len(new)}개: " + " / ".join(new[:4]) + " — 고치고 다시 점검하거나, 남길 것은 인계의 "
                "follow_ups 에 까닭과 함께 적는다")
    sig = hashlib.sha1(json.dumps(sorted(new), ensure_ascii=False).encode()).hexdigest()[:12]
    if run.get("design_block_sig") != sig:  # 처음 보는 남은 문제 — 한 번은 돌려보내 고치게 한다
        inbox.update_run(settings, run["id"], lambda r: r.update(design_block_sig=sig))
        return (f"새 화면에서만 나온 디자인 문제 {len(new)}개: " + " / ".join(new[:6]) + " — 고치고 다시 점검한다. 공통 부품에서 오는 것이면 "
                "그 부품을 쓰는 기존 화면을 compare 에 더해 다시 점검한다 (기존 화면에도 있으면 툴킷이 기존 문제로 가른다). 그래도 남겨야 하면 "
                "follow_ups 에 무엇을 왜 남기는지 적고 다시 인계한다 — 사용자는 인계에서 '새 화면에서만 나온 문제'로 그대로 본다")
    if not _items(follow_ups, 6):
        return "새 화면에서만 나온 디자인 문제를 남겼다 — follow_ups 에 무엇을 왜 남기는지 적는다"
    return None


def _design_checks(run: dict) -> list[dict]:
    return [e for e in inbox.events(settings, run["id"])[0] if e.get("kind") == "design_check"]


def _ui_without_screen(run: dict) -> str | None:
    """화면 요구사항을 화면 케이스로 확인했나 — 실험: 케이스 20개 중 14개가 저장 함수를 바로 불러 확인창을 거치지 않았는데,
    인계는 '저장 전에 확인을 묻습니다'라고 했고 실제 확인창은 상세 팝업 뒤에 가려져 있었다. 툴킷은 화면을 직접 돌리므로 화면 파일을
    바꾼 작업에서는 요구사항마다 화면 케이스가 있어야 한다 — 화면에서 확인할 수 없는 요구사항은 에이전트가 출처 칸에 screen: false 로
    밝힌다 (카드에 보인다). 어느 요구사항이 화면 동작인지 글을 읽어 짐작하지 않는다 (실험: '표시'·'보인다' 같은 낱말로 갈라 틀렸다)."""
    from beom_workbench.core import make, project, screens
    if not screens.available() or run.get("mode") != "request" or not run.get("project_id") or not run.get("port_id"):
        return None
    if not _ui_changes(run)[0] and not _screen_files((run.get("plan") or {}).get("steps") or []):
        return None
    meta = _req_meta(run)
    try:
        conn, _ = project.load(settings, run["project_id"])
        reqs = make.requirements(conn, run["port_id"])
    except Exception:  # noqa: BLE001 — 읽지 못하면 막지 않는다
        return None
    cases = _golden_cases(run)

    def reqs_of(c: dict) -> set[str]:
        r = c.get("req") or c.get("reqs") or []
        return {r} if isinstance(r, str) else {str(x) for x in r}

    on_screen = {r for c in cases if c.get("screen") for r in reqs_of(c)}
    # 에이전트가 출처와 함께 저장한 요구사항만 — 사용자가 앱에서 쓴 요구사항은 에이전트에게 화면 여부를 묻지 않는다
    missing = [f"{r['id']} '{str(r['text'])[:40]}'" for r in reqs
               if (meta.get(r["id"]) or {}).get("from") not in (None, "user") and (meta.get(r["id"]) or {}).get("screen") is not False
               and r["id"] not in on_screen]
    if not missing:
        return None
    return ("화면 케이스가 없는 요구사항: " + "; ".join(missing[:6]) + " — 화면을 바꾼 작업은 요구사항마다 screen 케이스를 하나 이상 둔다 "
            "(actions 로 눌러 본 뒤 visible 로 보여야 할 것을 확인. 함수·API 를 바로 부르는 케이스는 화면 동작의 근거가 아니다). 화면에서 "
            "확인할 수 없는 요구사항(저장 방식·API 응답 등)이면 wb_make_requirements 로 그 요구사항에 screen: false 를 적어 다시 저장한다")


def _design_coverage(run: dict, fp: str | None) -> tuple[set, int]:
    """디자인을 바꾸는 작업에서 지금 화면 파일 그대로(fp) 점검한 서로 다른 화면과 필요한 수. 실제로 다르게 보인 화면만 센다 — 실험: 한
    주소에서 메뉴로 화면을 바꾸는 앱에서 '/?p=product' 같은 주소를 붙여 같은 목록 화면을 다섯 번 찍고 다섯 곳이라고 했다. 그림이 같으면
    한 화면이다 (PC 크기 그림으로 가른다)."""
    from beom_workbench.core import conventions
    seen = {e.get("sig") or (e.get("page"), e.get("state")) for c in _design_checks(run) if fp and c.get("ui_fp") == fp
            for e in c.get("pages") or [] if not str(e.get("size") or "").startswith("모바일")}
    screens_n = sum(1 for f in conventions.ui_files(Path(run["target_root"]))[1] if f.suffix.lower() in conventions.SCREEN_EXTS)
    return seen, min(4, max(1, screens_n))


def _last_design_check(run: dict, fp: str | None = None) -> dict | None:
    """지금 화면 파일 그대로(지문이 같은) 마지막 디자인 점검 — 없으면 None."""
    fp = fp or _ui_changes(run)[1]
    if not fp:
        return None
    checks = [e for e in inbox.events(settings, run["id"])[0] if e.get("kind") == "design_check"]
    return next((e for e in reversed(checks) if e.get("ui_fp") == fp), None)


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
    # 실제로 생긴 의존성·빌드·툴킷 작업 폴더가 .gitignore 에 빠져 있으면 — 처음 git add . 에 수십 MB(node_modules·.beom/venv)가
    # 함께 들어간다 (실험: 새 프로젝트의 .gitignore 가 .beom/ 을 빼지 않았다)
    # 툴킷이 직접 더한다 — 툴킷 작업 폴더와 설치·빌드가 만든 폴더는 뜻을 따질 것 없이 빼는 것이라 돌려보낼 까닭이 없다
    loose = [d for d in (".beom", "node_modules", ".venv", "venv", "__pycache__") if (root / d).is_dir() and not _ignored(root, root / d / "_")]
    maybe = [d for d in ("target", "build", "dist", "bin", "obj") if (root / d).is_dir() and not _ignored(root, root / d / "_")]
    if maybe:  # 빌드 결과일 수도, 소스일 수도 있다 (Node CLI 의 bin/cli.js) — 툴킷이 정하지 않는다
        out.append(".gitignore 가 " + ", ".join(f"{d}/" for d in maybe) + " 를 빼지 않음 — 빌드 결과면 .gitignore 에 더하고, 소스면 그대로 둔다 "
                   "(소스면 인계의 risks 에 한 줄로 밝힌다)")
    from beom_workbench.core import workbranch
    if loose and ((root / ".gitignore").is_file() or workbranch.repo_top(str(root))):  # git 저장소가 아닌 폴더에는 만들지 않는다
        gi = root / ".gitignore"
        try:
            cur = gi.read_text(encoding="utf-8") if gi.is_file() else ""
            gi.write_text(cur + ("" if not cur or cur.endswith("\n") else "\n") + "".join(f"{d}/\n" for d in loose), encoding="utf-8")
            inbox.log(settings, run["id"], "note", text=".gitignore 에 " + ", ".join(f"{d}/" for d in loose) + " 를 더했습니다 (설치·빌드 결과와 작업 폴더)")
        except OSError:
            out.append(".gitignore 가 " + ", ".join(f"{d}/" for d in loose) + " 를 빼지 않음 — .gitignore 에 더한다")
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
        # 폴더 줄(node_modules/)은 그 안의 모든 파일에 걸린다
        if fnmatch.fnmatch(f.name, pat) or fnmatch.fnmatch(rel, pat) or any(fnmatch.fnmatch(p, pat) for p in Path(rel).parts[:-1]):
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
        return brief.check(run["target_root"], _policy_docs(run), clean, _user_words(run))
    return brief.check(run["target_root"], _policy_docs(run), run.get("brief_changes") if _revision_pass(run) else None, _user_words(run))



def _revision_pass(run: dict) -> bool:
    """인계까지 끝난 기획에 고칠 점이 와서 도는 라운드인가."""
    return bool(run.get("revision")) and bool(run.get("revision_round")) and run.get("revision_round") == run.get("round")


def _wireframe_defects(run: dict) -> dict[str, list[str]]:
    """목업마다 고칠 결함(fix) — 넘침·잘림·글자 꺾임·대비·작은 누르는 곳. 화면 엔진이 없으면 보지 않는다(빈 결과)."""
    from beom_workbench.core import brief, screens
    wf = brief.folder(run["target_root"]) / "wireframes"
    if not screens.available() or not wf.is_dir() or not any(wf.glob("S*.html")):
        return {}
    try:
        out = brief.screens(run["target_root"], None, False, audit=True)
    except Exception:  # noqa: BLE001 — 찍지 못하면 인계를 막지 않는다 (찍기 자체의 오류는 wb_brief_screens 가 알린다)
        return {}
    bad = {}
    for s_ in out.get("shots") or []:
        if s_.get("audit"):
            fix, _adv = _design_findings(s_["audit"])
            fix = [x for x in fix if not x.startswith("점검 결과를 읽지 못함")]
            if fix:
                bad[s_.get("file") or "?"] = fix
    return bad


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
    # 넘침·잘림·글자 꺾임·대비 같은 결함(fix)은 누구의 모양을 따르든 결함이다 — 늘 본다. 기존 제품을 따르는 기획이면 취향 쪽(advice:
    # 'AI 티'·글꼴 가짓수)은 그 제품이 정하므로 뺀다 (예전엔 기존 코드가 있으면 점검을 통째로 꺼서 표가 화면 밖으로 넘친 목업이 인계됐다)
    own = not ((run.get("existing") or {}).get("screens") or (run.get("existing") or {}).get("code")) and not run.get("context_project")
    out = brief.screens(run["target_root"], files, mobile, audit=True)
    if True:
        design = {}
        for s_ in out["shots"]:
            if s_.get("audit"):
                fix, adv = _design_findings(s_.pop("audit"))
                adv = adv if own else []
                if fix or adv:
                    design[s_["file"]] = {"fix": fix[:8], "advice": adv[:8]}
        if design:
            out["design"] = design
            out["design_note"] = "목업도 실제 화면처럼 읽힌다 — fix 는 고치고, advice 의 'AI 티'는 피한다 (취향인 것은 까닭이 있으면 둔다)"
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
    if not app and not files and (url or paths):
        url, cmd = _launch_fill(run, url, cmd)
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


# 'AI 티' 검출 이름 → 사용자·에이전트가 읽는 말 (무엇으로 바꾸면 되는지까지)
TELLS = {
    "eyebrow": "제목 위의 작은 강조 라벨 — 지우고 제목이 스스로 말하게 한다",
    "side_stripe": "둥근 상자 한쪽의 색 띠 — 전체 테두리·옅은 바탕색·앞쪽 아이콘으로 바꾼다",
    "gradient_text": "그라데이션 글자 — 굵기·크기로 강조한다",
    "glass": "장식용 유리(흐림) 효과 — 필요한 곳이 아니면 뺀다",
    "hard_shadow": "흐림 없는 굵은 오프셋 그림자 — 부드러운 그림자나 테두리로",
    "glow": "색 빛 번짐 그림자 — 깊이를 주는 그림자가 아니라 장식이다",
    "nested_cards": "카드 안의 카드 — 안쪽은 구분선·여백으로 나눈다",
    "card_grid": "같은 모양 아이콘·제목·글 카드 나열 — 내용에 맞는 배치(목록·표·크기가 다른 덩어리)로",
    "emoji_icon": "이모지·기호를 아이콘으로 씀 — 한 벌로 그린 SVG 아이콘으로",
    "bounce": "튀는(오버슛) 움직임 — 부드럽게 멈추는 ease-out 으로",
    "system_display": "가장 큰 제목이 기기 기본 글꼴 — 분야 성격이 드러나는 제목 글꼴을 불러온다",
    "all_centered": "글 덩어리 대부분이 가운데 정렬 — 읽는 글은 왼쪽 정렬로",
    "no_favicon": "파비콘이 없음 — 브라우저 탭에 기본 아이콘이 뜬다. 화면의 주 색·모양으로 그린 작은 SVG 파비콘을 넣는다",
    "emoji_favicon": "이모지 글자 파비콘 — 화면의 주 색·모양으로 그린 SVG 파비콘으로",
    "mixed_icons": "아이콘 모양이 섞임 — 한 벌(같은 선 굵기·같은 선/채움 방식)로 맞춘다",
    "favicon_off_palette": "파비콘 색이 화면에 쓰인 색과 다름 — 화면의 주 색으로 맞춘다",
}
PAGE_ONLY_TELLS = ("no_favicon", "emoji_favicon", "favicon_off_palette")  # 시안(그림 한 장)에는 묻지 않는다


def _design_findings(audit: dict | None) -> tuple[list[str], list[str]]:
    """점검 결과 → (고칠 것, 참고). 고칠 것: WCAG 대비 미달·24px 미만 누르는 곳·가로 넘침·잘린 글자·겹친 버튼·12px 미만 글자·
    흐린 입력칸 경계(3:1)·보이지 않는 포커스·불러오지 못한 글꼴. 참고: 흐린 안내 글자(placeholder)·같은 버튼의 다른 글자 크기."""
    if not audit:
        return ["점검 결과를 읽지 못함 (페이지가 이동 중이었을 수 있음)"], []
    iss, var = audit.get("issues") or {}, audit.get("variety") or {}
    fix, advice = [], []
    for c in iss.get("contrast") or []:
        fix.append(f"대비 부족 {c['ratio']}:1 (필요 {c['need']}:1) — {c['el']} '{c['text']}'")
    for t in iss.get("tiny_targets") or []:
        hint = " — 체크박스·라디오는 <label> 로 감싸면 글자까지 누르는 곳이 된다(목업 키트: <label class=\"ck\">)" if "ck" in t["el"] or t["el"].startswith(("input", "#")) and t.get("text") == "on" else ""
        fix.append(f"누르기 너무 작음 {t['w']}×{t['h']}px (24px 이상) — {t['el']} '{t['text']}'{hint}")
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
    for c in iss.get("control_border") or []:
        fix.append(f"입력칸 경계가 흐림 {c['ratio']}:1 ({c['line']} — 테두리나 칸 바탕이 바깥 바탕과 3:1 이상) — {c['el']} '{c['text']}'")
    for c in iss.get("focus_hidden") or []:
        fix.append(f"포커스 표시가 없음 — outline 을 없앴는데 대신할 표시가 없음 (외곽선·테두리·그림자를 3:1 이상으로) — {c['el']} '{c['text']}'"
                   if not c.get("ratio") else
                   f"포커스 표시가 흐림 {c['ratio']}:1 ({c['cue']} — 3:1 이상) — {c['el']} '{c['text']}'")
    for f in iss.get("font_missing") or []:
        fix.append(f"글꼴이 안 불러와짐 — '{f['font']}' 를 쓰게 했지만 {f['why']} — 이름 붙인 글꼴이 하나도 안 보여 기본 글꼴로 보인다 ({f['count']}곳, 예: {f['el']} "
                   f"'{f['text']}'). 불러오는 곳(글꼴 링크·@font-face)과 이름·굵기를 맞춘다. 받을 수 없는 환경이면(인터넷 없이 여는 파일·막힌 주소) "
                   "글꼴 파일을 폴더에 넣어 @font-face 로 쓰거나, 대부분의 컴퓨터에 있는 글꼴로 바꾼다 — 대체 글꼴로 보여도 어색하지 않게 "
                   "font-family 목록 뒤쪽도 고른다")
    for f in iss.get("font_fallback") or []:
        advice.append(f"글꼴 대신 보임 — {', '.join(repr(x) for x in f.get('missing') or [f['font']])} 가 이 컴퓨터에서 보이지 않아 "
                      f"'{f['shown']}' 로 보인다 ({f['count']}곳, 예: {f['el']} '{f['text']}') — 다른 컴퓨터용 이름이면 괜찮다. "
                      "사용자가 고른 글꼴이라면 그대로 쓸 수 없다는 것과 대신 쓴 글꼴을 알린다")
    for t in iss.get("small_targets") or []:
        advice.append(f"모바일에서 누르기 작음 {t['w']}×{t['h']}px (44px 권장) — {t['el']} '{t['text']}'")
    for c in iss.get("placeholder") or []:
        advice.append(f"안내 글자(placeholder)가 흐림 {c['ratio']}:1 (4.5:1 권장){' — 꺼진 칸이라도 무엇을 넣는 칸인지는 읽혀야 한다' if c.get('off') else ''}"
                      f" — {c['el']} '{c['text']}'")
    for m in iss.get("button_mismatch") or []:
        a, b = m["a"], m["b"]
        advice.append(f"같은 버튼(.{m['cls']})인데 글자가 다름 — {a['el']} '{a['text']}' {a['fs']}px·{a['fw']} / {b['el']} '{b['text']}' "
                      f"{b['fs']}px·{b['fw']} (font: inherit 가 둘레 글자를 받았는지 본다)")
    kinds: dict = {}
    for t in iss.get("tells") or []:  # 같은 종류는 한 줄로 — 첫 곳과 개수
        kinds.setdefault(t.get("kind"), []).append(t)
    for kind, ts in kinds.items():
        t = ts[0]
        advice.append(f"AI 티 — {TELLS.get(kind, kind)}" + (f" ({t['detail']})" if t.get("detail") else "")
                      + (f" — {t['el']} '{t['text']}'" if t.get("el") else "") + (f" 외 {len(ts) - 1}곳" if len(ts) > 1 else ""))
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
    catalog: 문구 키와 값(공통 묶음 먼저), namespaces(주석에 적힌 키 규칙), lookups(문구를 읽는 함수). styles: 파일별 클래스와
    범위(global·scoped·page). constants: 여러 파일이 쓰는 상수·값 목록. data_helpers: 목업·API·서비스 모듈의 함수.
    server: 엔티티별 서비스·매퍼·쿼리. parts: 한 곳만 쓰는 부품까지 — 받는 옵션(props·emits·기본값)과 함께. unstyled: 화면·공통
    코드가 쓰는데 스타일 규칙이 없는 클래스. styles[].rules: 공통 스타일 규칙의 내용. style_repeats: 여러 화면에 따로 있는 같은
    클래스(공통 후보). catalog.groups: 공통 키와 한 대상 전용 키. messages·filters: 문구 목록이 없을 때의 문구와 템플릿 필터.
    새 키·클래스·상수·함수·쿼리를 만들기 전에 여기서 같은 것을 찾는다 (있는 것만 나온다).
    비슷한 화면을 열어 읽고, 같은 부품을 같은 방식으로 쓴다. 계획(wb_plan_set)의 follows 에 따를 규칙과 근거를 남긴다."""
    from beom_workbench.core import conventions

    run = _run()
    if run is None:
        return {"ok": False, "error": "자동 실행 밖"}
    res = conventions.scan(Path(run["target_root"]), near=near, like=like)
    inbox.log(settings, run["id"], "ui_conventions", near=[str(x) for x in near or []][:10])
    if not res["shared"] and not res["events"]:
        return {"ok": True, **res, "note": "기존 화면 코드가 없음 — 새로 만드는 화면이면 '화면 디자인' 절대로"}
    return {"ok": True, **res, "next": "similar 의 화면들을 열어 읽고 follows=[{rule, evidence:[파일:줄…]}] 로 계획에 남긴다"}


@mcp.tool()
@_record_refusal
def wb_design_check(files: list | None = None, url: str | None = None, paths: list | None = None,
                    cmd: str | None = None, actions: list[dict] | None = None, compare: list | None = None) -> dict:
    """디자인 점검 — 화면을 실제 브라우저 엔진으로 모바일(390px)·PC(1280px) 두 크기로 열어, 계산된 스타일과 위치로 잰다:
    글자와 바탕의 대비(WCAG 4.5:1, 큰 글자 3:1), 누르는 곳 크기(24px 미만은 고칠 것, 모바일 44px 권장), 가로 넘침, 잘린 글자,
    겹친 버튼, 12px 미만 글자, 글꼴·글자 크기·색이 너무 많은지. fix 는 고칠 것, advice 는 참고. 스크린샷도 함께 남긴다.
    인자 없이 부르면 툴킷이 고른다 — 화면 케이스가 여는 화면(같은 경로·같은 동작), 검증(wb_port_verify) 때 화면을 연 서버, 견줄 기존 화면
    (작업 전부터 있던 화면 중 이번에 고치지 않은 것), 디자인을 바꾸는 작업이면 바뀐 스타일이 걸리는 기존 화면까지. 보통은 이렇게 부른다.
    직접 줄 때: files=["index.html"] 또는 url·paths(·cmd — 비우면 검증 때의 서버 명령), actions 로 눌러 본 뒤의 화면도.
    화면마다 눌러 열어야 하면 (한 주소에서 메뉴로 화면을 바꾸는 앱·팝업) paths·files 의 항목을 {"path"|"file": …, "actions": […]} 로 —
    한 번의 점검에 여러 화면을 함께 넣는다. 화면 수는 실제로 다르게 보인 화면만 센다 (같은 화면을 다른 주소로 열면 하나다).
    compare: 같은 부품을 쓰는 기존 화면 — files 모드면 파일, url 모드면 경로. 눌러 열어야 보이면 {"path"|"file": …, "actions": […]}.
    툴킷이 실행 전부터 있던 화면인지 확인하고 함께 재서, 새 화면의 fix 를 '기존 화면에도 있는 것'(existing_defects — 기존 제품의
    고칠거리로 사용자에게 알린다)과 '새 화면에서만 나온 것'(new_issues — 고친다)으로 나눈다."""
    from beom_workbench.core import screens

    run = _run()
    if run is None:
        return {"ok": False, "error": "자동 실행 밖"}
    if not screens.available():
        return {"ok": False, "error": "이 설치에는 화면 엔진(PySide6)이 없음"}
    root = Path(run["target_root"]).resolve()
    picked = None
    if not files and not url and not paths:
        # 무엇을 어디서 잴지 — 툴킷이 가진 사실로 고른다: 화면 케이스가 여는 화면(같은 경로·같은 동작), 검증 때 쓴 서버, 작업 전부터
        # 있던 화면 (실험: 에이전트가 고르니 화면 하나만 재거나, 점검하는 화면 자신을 비교로 넣거나, 없는 주소를 열었다)
        picked = _auto_design_targets(run, root)
        if picked is None:
            return {"ok": False, "error": "점검할 화면을 찾지 못했다 — 화면 케이스가 없고 바뀐 화면 파일도 없다. files 또는 url·paths 를 준다"}
        files, url, paths, cmd = picked["files"], picked["url"], picked["paths"], picked["cmd"]
        compare = compare or picked["compare"]
    elif url or paths:
        url, cmd = _launch_fill(run, url, cmd)
    # 지난 점검 뒤로 바뀐 것이 없고 같은 것을 다시 재면 결과도 같다 — 실험: 같은 상태를 6번 다시 쟀다 (한 번에 수십 초)
    import hashlib
    work = hashlib.sha256(json.dumps([files, url, paths, cmd, actions, compare], sort_keys=True, default=str).encode())
    for f in sorted(_changed_files(run)):
        try:
            work.update(f.encode() + b"\0" + (root / f).read_bytes() + b"\0")
        except OSError:
            work.update(f.encode() + b"\0-\0")
    work_sig = work.hexdigest()[:16]
    last = (_design_checks(run) or [None])[-1]
    if last and last.get("work_sig") == work_sig and last.get("result"):
        return {**last["result"], "cached": True,
                "note": "지난 점검 뒤로 바뀐 파일이 없어 다시 재지 않았다 — 같은 결과다. 고친 뒤 점검한다 (같은 점검을 되풀이하지 않는다)"}
    out_dir = root / ".beom" / "shots"
    out_dir.mkdir(parents=True, exist_ok=True)
    targets: list[tuple[str, dict]] = []
    server = None
    before = None
    from beom_workbench.core import port
    srv = None
    try:
        cmd0, url0 = cmd, url
        if url:
            if cmd:
                inbox.gate_commands(settings, {"screen_server": cmd})
            cmd, url = port.fill_port(cmd, url)
            port.check_url(url)
            before = port._files(root)
            if cmd:
                server = port._Server(port._Side("target", root, root / ".beom"), cmd, url)
            for item in (paths or ["/"])[:6]:
                spec_in = item if isinstance(item, dict) else {"path": item}
                pth = "/" + str(spec_in.get("path") or "/").lstrip("/")
                acts = spec_in.get("actions") if isinstance(spec_in.get("actions"), list) else None
                targets.append((pth + _acts_label(acts), {"path": pth}, acts))
            base = url
        else:
            if not files:
                return {"ok": False, "error": "files(HTML) 또는 url(서버 주소)이 필요함"}
            for item in files[:6]:
                spec_in = item if isinstance(item, dict) else {"file": item}
                f = str(spec_in.get("file") or "")
                acts = spec_in.get("actions") if isinstance(spec_in.get("actions"), list) else None
                # 'notice-detail.html?id=1' — 정적 화면도 주소 뒤의 질의·조각으로 다른 내용을 보인다 (화면 케이스처럼 받는다)
                name, tail = re.match(r"^([^?#]*)(.*)$", str(f)).groups()
                p = (root / name).resolve()
                if not p.is_relative_to(root) or not p.is_file():
                    return {"ok": False, "error": f"대상 폴더 안의 파일이 아님: {f}"}
                rel = p.relative_to(root).as_posix() + tail
                targets.append((rel + _acts_label(acts), {"file": rel}, acts))
            srv = screens.serve_folder(root)
            srv.__enter__()
            base = None
        cmp_targets, cmp_bad = _compare_targets(run, root, compare, url_mode=bool(url), targets=targets, actions=actions)
        pages = []
        import hashlib
        import time
        # 점검마다 다른 이름 — 같은 경로를 다른 actions 로 찍으면 앞 그림을 덮어써 무엇을 봤는지 남지 않았다
        stamp = time.strftime("%H%M%S") + "-" + hashlib.sha1(json.dumps(actions or [], sort_keys=True).encode()).hexdigest()[:4]
        jobs = [(label, spec, list(actions or []) if acts is None else list(acts), False) for label, spec, acts in targets] + \
               [(label, spec, acts, True) for label, spec, acts in cmp_targets]
        for label, spec, acts, is_cmp in jobs:
            for mobile in (True, False):
                name = (("cmp-" if is_cmp else "check-") + (re.sub(r"[^\w-]+", "_", label).strip("_")[:50] or "root") + f"-{stamp}"
                        + ("-mobile" if mobile else ""))
                pages.append((label, mobile, is_cmp, screens.page_spec({**spec, "mobile": mobile, "wait_ms": 700, "audit": True,
                                                                        "actions": acts},
                                                                       base, root, str(out_dir / f"{name}.png"),
                                                                       file_base=srv.base if srv else None)))
        got = screens.capture([pg for *_, pg in pages])
    finally:
        if server:
            server.close()
        if srv:
            srv.__exit__(None, None, None)
        if before is not None:
            port._drop_new(root, before, keep=set())
    report, cmp_report = [], []
    scope = design_scope(run)
    measured = list(zip(pages, got))
    from beom_workbench.core import runner as runner_core
    if url and runner_core.screens_at_start(run) and scope not in ("restyle", "redesign") and _changed_files(run):
        extra, bad = _baseline_pages(run, root, cmd0, url0, targets, actions, out_dir, stamp)
        measured += extra
        cmp_bad = list(cmp_bad or []) + bad
    for (label, mobile, is_cmp, _pg), r in measured:
        fix, advice = _design_findings(r.get("audit"))
        layout = []
        if scope == "restyle" and not is_cmp:  # 느낌만 바꾸는 작업 — 배치 문제는 배치를 바꿔 고치지 않는다 (실험: 바꾸다 되돌려졌다)
            layout = [f for f in fix if LAYOUT_FIX.search(f)]
            fix = [f for f in fix if f not in layout]
        row = {"page": label, "size": "모바일 390px" if mobile else "PC 1280px", "shot": r.get("out") if r.get("ok") else None,
               "fix": fix[:20], "advice": advice[:10], **({"layout_kept": layout[:10]} if layout else {}),
               **({"errors": r["errors"][:3]} if r.get("errors") else {})}
        (cmp_report if is_cmp else report).append(row)
    inbox.log(settings, run["id"], "screen_shots", shots=[x["shot"] for x in report + cmp_report if x["shot"]])
    total = sum(1 for x in report for f in x["fix"] if not f.startswith(UNREAD))  # 잴 수 없었던 화면은 고칠 것이 아니다
    existing_defects, new_issues = split_design_fixes(report, cmp_report)
    layout_kept = list(dict.fromkeys(f"{x['page']} {x['size']}: {f}" for x in report for f in x.get("layout_kept") or []))
    # 무엇을 봤고 무엇이 나왔는지 남긴다 — 인계 때 '마지막 수정 뒤에 점검했나'를 이 지문으로 견준다
    from beom_workbench.core import runner as runner_core
    existing = runner_core.screens_at_start(run)  # 견줄 기존 화면이 있었나
    if not total:
        nxt = "고칠 것 없음 — advice 를 보고 다듬는다."
    elif not existing or scope in ("restyle", "redesign"):
        nxt = ("fix 를 모두 고친 뒤 다시 점검한다 — 대비는 토큰의 색을, 크기는 패딩·최소 높이를 고친다"
               + (" (디자인을 바꾸는 작업이라 공통 스타일에서 오는 것도 고친다 — 계획의 shared_changes 로 승인받은 파일에서)" if existing else "") + ".")
    else:
        nxt = (f"새 화면에서만 나온 문제 {len(new_issues)}개는 고치고 다시 점검한다. 기존 화면에도 똑같이 있는 문제 {len(existing_defects)}개는 "
               "원래 있던 공통 부품·전역 스타일의 문제라 고치지 않는다 — 툴킷이 기존 제품의 고칠거리로 사용자에게 알린다."
               + ("" if cmp_report else " 지금은 compare 가 없어 무엇이 기존 문제인지 툴킷이 가르지 못했다 — 같은 부품을 쓰는 기존 화면을 "
                                         "compare 에 넣어 다시 점검한다 (기존 문제라는 말만으로는 넘기지 않는다)."))
    same = (" 기존 화면(compare)과 버튼·입력 높이, 글자 크기, 알림·확인창 위치, 목록 순서가 같은지도 그림으로 본다 — 다르면 새 화면을 기존 "
            "화면에 맞춘다 (새 화면에만 다른 크기·덮어쓰는 스타일을 두지 않는다)." if existing and scope not in ("restyle", "redesign") else "")
    if layout_kept:
        nxt += (f" 배치 문제 {len(layout_kept)}개는 느낌만 바꾸는 작업이라 배치를 바꿔 고치지 않는다 — 바꾼 토큰(글자 크기·간격 등) 때문에 생긴 것이면 "
                "토큰을 고치고, 원래 있던 것이면 그대로 둔다 (인계에 '확인 못 한 배치 문제'로 사용자에게 보인다).")
    # 'AI 티' — 따를 기존 디자인이 없을 때(새로 만들거나 디자인을 바꾸는 작업)만 알린다. 기존 화면을 따르는 작업이면 그것이 기존 제품의 모습이다
    # 레퍼런스 실행은 원본이 화면의 모습을 정한다 — 원본과 다르게 바꾸라고 하지 않는다 (시안 단계가 없는 것과 같은 까닭)
    own_look = run.get("mode") == "request" and (not existing or scope in ("restyle", "redesign"))
    tells = list(dict.fromkeys(a for x in report for a in x["advice"] if a.startswith("AI 티"))) if own_look else []
    if own_look:
        tells += _detector_tells(run, root, tells)
    if tells:
        nxt += (f" AI 티로 읽히는 것 {len(tells)}개(ai_tells) — 모델이 기본값으로 만드는 모습이라 사람이 디자인한 화면으로 보이지 않는다. 적힌 대로 바꾸고 "
                "다시 점검한다 (분야에 꼭 맞아 일부러 쓴 것이면 남겨도 된다 — 사용자가 인계에서 본다).")
    out = {"ok": True, "fix_count": total, "pages": report, "next": nxt + same +
           " 목록·표가 비어 있으면 actions 로 기간·검색을 채워 데이터가 보이는 상태도 점검한다. 그림(shot)을 열어 직접 본다."}
    if tells:
        out["ai_tells"] = tells[:12]
    if cmp_report:
        out.update(compared=cmp_report, existing_defects=existing_defects, new_issues=new_issues)
    if cmp_bad:
        out["compare_refused"] = cmp_bad
    ui_fp = _ui_changes(run)[1]
    inbox.log(settings, run["id"], "design_check", ui_fp=ui_fp, fix_count=total, compared=[x["page"] for x in cmp_report][::2],
              existing_defects=existing_defects[:20], new_issues=new_issues[:20], layout_kept=layout_kept[:20],
              pages=[{"page": x["page"], "size": x["size"], "state": stamp.split("-")[-1], "fix": x["fix"][:8], "shot": x["shot"],
                      **({"sig": _shot_sig(x["shot"])} if x.get("shot") else {})} for x in report][:12],
              ai_tells=tells[:12], work_sig=work_sig,
              result={k: out[k] for k in ("ok", "fix_count", "pages", "next", "compared", "existing_defects", "new_issues", "compare_refused", "ai_tells") if k in out})
    if existing and scope in ("restyle", "redesign") and ui_fp:
        # 인계 때에야 '몇 곳뿐'이라고 알리면 한 번을 더 쓴다 (실험: 디자인을 바꾸는 실행마다) — 점검할 때 바로 센 값을 준다
        seen, need = _design_coverage(run, ui_fp)
        out["screens_checked"] = {"now": len(seen), "need": need}
        if len(seen) < need:
            out["next"] += (f" 지금 화면 파일 그대로 점검한 서로 다른 화면 {len(seen)}곳 / 인계에 필요한 {need}곳 — 남은 화면을 paths·files 에 "
                            "{\"path\"|\"file\": …, \"actions\": […]} 로 더해 점검한다 (파일을 또 고치면 처음부터 다시 센다).")
    return out


LAYOUT_FIX = re.compile(r"^(?:가로로 넘침|글자가 한두 글자씩 줄바뀜|누르는 곳끼리 겹침)")


def _acts_label(acts: list | None) -> str:
    """눌러 연 화면의 이름 꼬리 — '/ (aside button:nth-of-type(2))' 처럼 무엇을 눌렀는지 (같은 주소의 다른 화면을 가른다)."""
    last = next((str(a.get(k)) for a in reversed(acts or []) if isinstance(a, dict)
                 for k in ("click", "dblclick", "press", "fill") if a.get(k)), "")
    return f" ({last[:30]})" if last else ""


def _shot_sig(path: str | None) -> str | None:
    """그림의 지문 — 같은 화면을 다른 주소로 찍은 것을 한 곳으로 센다."""
    import hashlib
    try:
        return hashlib.sha1(Path(path).read_bytes()).hexdigest()[:12] if path else None
    except OSError:
        return None


def _rel(f) -> str:
    """'./a/b' → 'a/b' — 앞의 './' 만 뗀다 (lstrip('./') 는 .gitignore·.storybook 의 점까지 뗐다)."""
    return re.sub(r"^(?:\./)+", "", str(f).replace("\\", "/"))


def _launch_fill(run: dict, url: str | None, cmd: str | None) -> tuple[str | None, str | None]:
    """화면을 여는 서버 — 비운 칸은 검증(wb_port_verify) 때 화면을 연 주소·명령으로 채운다 (화면 찍기·디자인 점검·검증이 같은 서버로 연다)."""
    la = run.get("screen_launch") or {}
    url = url or la.get("url")
    if url and not cmd and la.get("url") == url:
        cmd = la.get("cmd")
    return url, cmd


def _auto_design_targets(run: dict, root: Path) -> dict | None:
    """디자인 점검의 대상 — 화면 케이스가 여는 화면(경로·동작 그대로), 화면을 연 서버(검증 때 남긴 것), 견줄 기존 화면(작업 전부터 있던
    화면 중 이번에 고치지 않은 것), 디자인을 바꾸는 작업이면 바뀐 스타일이 걸리는 기존 화면까지. 글을 읽지 않고 기록으로 고른다."""
    from beom_workbench.core import conventions, runner as runner_core
    specs: list[dict] = []
    for c in _golden_cases(run) if run.get("port_id") else []:
        s = c.get("screen")
        if not isinstance(s, dict) or s.get("app") or s.get("mobile"):
            continue
        kind = "file" if s.get("file") else "path" if s.get("path") else None
        if not kind:
            continue
        acts = s.get("actions") if isinstance(s.get("actions"), list) and s.get("actions") else None
        item = {kind: str(s[kind]), **({"actions": acts} if acts else {})}
        if item not in specs:
            specs.append(item)
    # 화면마다 먼저 처음 모습, 그다음 눌러 본 모습 — 같은 화면만 여러 번 재지 않게
    specs.sort(key=lambda x: (bool(x.get("actions")),))
    launch = run.get("screen_launch") or {}
    ui = [f for f in _ui_changes(run)[0] if f.lower().endswith(conventions.SCREEN_EXTS)]
    existing = [s for s in ((run.get("existing") or {}).get("screens") or []) if (root / s).is_file()]
    if any("path" in s for s in specs) and launch.get("url"):
        targets = [s for s in specs if "path" in s][:6]
        return {"files": None, "url": launch["url"], "paths": targets, "cmd": launch.get("cmd"), "compare": None}
    files = [s for s in specs if "file" in s]
    for s in specs:  # 서버 없이 주소로 연 정적 화면 — 파일로 연다
        if "path" in s:
            name = s["path"].split("?")[0].split("#")[0].lstrip("/") or "index.html"
            if (root / name).is_file():
                files.append({"file": s["path"].lstrip("/") or "index.html", **({"actions": s["actions"]} if s.get("actions") else {})})
    files += [{"file": f} for f in ui if not any(x["file"].split("?")[0] == f for x in files)]
    scope = design_scope(run)
    had = runner_core.screens_at_start(run)
    changed = set(_changed_files(run))
    if had and scope in ("restyle", "redesign"):
        files += [{"file": s} for s in existing if not any(x["file"].split("?")[0] == s for x in files)]
    if not files:
        return None
    files = files[:6]
    shown = {x["file"].split("?")[0] for x in files}
    compare = [s for s in existing if s not in shown and s not in changed][:2] if had and scope not in ("restyle", "redesign") else None
    return {"files": files, "url": None, "paths": None, "cmd": None, "compare": compare or None}


def _compare_targets(run: dict, root: Path, compare: list | None, url_mode: bool, targets: list | None = None,
                     actions: list | None = None) -> tuple[list, list[str]]:
    """compare 로 받은 기존 화면 — 실행 전부터 있던 화면만 (툴킷이 확인한다: 파일은 시작 전 체크포인트에, 경로는 시작 전 코드에 있었나)."""
    from beom_workbench.core import tasks
    out, bad = [], []
    testing = {str(t[1].get("path") or t[1].get("file") or t[0]) for t in (targets or [])}
    changed = set(_ui_changes(run)[0]) | set(_changed_files(run))
    for c in (compare or [])[:4]:
        spec = c if isinstance(c, dict) else ({"path": c} if url_mode else {"file": c})
        acts = list(spec.get("actions") or [])
        if url_mode:
            pth = "/" + str(spec.get("path") or "/").lstrip("/")
            # 한 주소에서 메뉴를 눌러 화면을 바꾸는 앱(SPA) — 같은 주소라도 누르는 순서가 다르면 다른 화면이다 (실험: Vue 관리자의 기존 회원
            # 화면을 '점검하는 화면 자신'으로 거절해 인계를 못 하고 멈췄다)
            if pth in testing and (not acts or acts == list(actions or [])):
                bad.append(f"{pth} (점검하는 화면 자신 — 다른 기존 화면이어야 한다)")
                continue
            if not _route_existed(run, pth):
                bad.append(f"{pth} (실행 전 코드에서 이 경로를 찾지 못함)")
                continue
            # 같은 주소의 다른 화면은 이름에 누른 것을 붙여 가른다 (인계의 '함께 잰 기존 화면'에 무엇을 봤는지 보이게)
            last = next((str(a.get(k)) for a in reversed(acts) if isinstance(a, dict) for k in ("click", "text", "select") if a.get(k)), "")
            out.append((pth + (f" ({last[:30]})" if pth in testing and last else ""), {"path": pth}, acts))
        else:
            rel = _rel(spec.get("file") or "")
            p = (root / rel).resolve()
            if not rel or not p.is_relative_to(root) or not p.is_file():
                bad.append(f"{rel} (대상 폴더 안의 파일이 아님)")
                continue
            rel = p.relative_to(root).as_posix()
            if tasks._existed(settings, run, rel) is not True:
                bad.append(f"{rel} (실행 전부터 있던 파일이 아님)")
                continue
            if rel in testing or rel in changed:
                # 고친 화면은 지금 모습이 기존 모습이 아니다 — 시작 전 모습을 꺼내 잰다. 거절했더니 에이전트가 비교 대상으로 쓰려고
                # 요청한 변경(메뉴 링크)을 지웠다 (실험)
                old = _baseline_file(run, root, rel)
                if old:
                    out.append((rel + " (시작 전)", {"file": old}, acts))
                else:
                    bad.append(f"{rel} (이번에 고친 화면인데 시작 전 모습을 꺼내지 못함 — 손대지 않은 다른 기존 화면을 고른다. "
                               "비교하려고 요청한 변경을 되돌리지 않는다)")
                continue
            out.append((rel, {"file": rel}, acts))
    if url_mode and bad:
        bad.append("비교할 기존 화면을 맞추려고 요청한 변경을 되돌리지 않는다 — 이번에 손대지 않은 다른 기존 화면을 고른다")
    return out, bad


def _baseline_dir(run: dict, root: Path) -> Path | None:
    """시작 전 체크포인트의 대상 폴더를 .beom/tmp/baseline-… 에 푼다 (스타일·스크립트도 함께 — 그때 모습 그대로)."""
    import io
    import tarfile
    from beom_workbench.core import checkpoints, tasks
    cid = tasks._start_checkpoint(settings, run)
    if not cid:
        return None
    dest = root / ".beom" / "tmp" / f"baseline-{cid[:10]}"
    if not (dest / ".done").is_file():
        try:
            data = checkpoints._git(inbox.run_dir(settings, run["id"]) / "shadow.git", root, "archive", "--format=tar", cid,
                                    text=False).stdout
            dest.mkdir(parents=True, exist_ok=True)
            with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                tf.extractall(dest, filter="data")
            (dest / ".done").write_text(cid, encoding="utf-8")
        except Exception:  # noqa: BLE001 — 꺼내지 못하면 시작 전 모습으로는 견주지 못한다
            return None
    return dest


def _baseline_file(run: dict, root: Path, rel: str) -> str | None:
    """시작 전 모습의 rel 파일 — 대상 폴더 기준 경로 (.beom/tmp/baseline-…/rel)."""
    dest = _baseline_dir(run, root)
    p = dest / rel if dest else None
    return p.relative_to(root).as_posix() if p is not None and p.is_file() else None


def _baseline_pages(run: dict, root: Path, cmd: str | None, url: str | None, targets: list, actions: list | None,
                    out_dir: Path, stamp: str) -> tuple[list, list[str]]:
    """서버 화면 — 이번에 고친 기존 화면은 지금 모습이 기존 모습이 아니다. 시작 전 코드를 따로 풀어 같은 서버 명령으로 띄우고 같은
    경로·동작을 재서 견준다 (실험 142: 고친 회원 목록의 원래 있던 모바일 가로 넘침이, 다른 화면에 없다는 이유로 '새 문제'로 갈려
    에이전트의 '기존 공통 표 문제' 설명과 어긋났다). 의존성은 대상 폴더 것을 그대로 (파이썬 .beom/venv, Node 는 위 폴더의 node_modules).
    → (재는 일감, 못 한 까닭)"""
    from beom_workbench.core import execenv, port, screens
    want = [(label, spec, acts) for label, spec, acts in targets if "path" in spec and _route_existed(run, spec["path"])]
    if not want or not cmd or not url:
        return [], []
    dest = _baseline_dir(run, root)
    if dest is None:
        return [], ["시작 전 모습을 꺼내지 못해 고친 기존 화면을 그때 모습과 견주지 못함"]

    class _Before(port._Side):
        def env(self):  # 의존성·도구 버전은 대상 폴더 기준 (풀어 둔 폴더에는 .beom/venv 가 없다)
            return execenv.command_env(root)
    c2, u2 = port.fill_port(cmd, url)
    pages = []
    server = None
    try:
        server = port._Server(_Before("baseline", dest, root / ".beom"), c2, u2)
        for label, spec, acts in want:
            for mobile in (True, False):
                slug = re.sub(r"[^\w-]+", "_", label).strip("_")[:50] or "root"
                name = f"before-{slug}-{stamp}" + ("-mobile" if mobile else "")
                pages.append((label + " (시작 전)", mobile, True, screens.page_spec(
                    {**spec, "mobile": mobile, "wait_ms": 700, "audit": True, "actions": list(actions or []) if acts is None else list(acts)},
                    u2, root, str(out_dir / f"{name}.png"))))
        got = screens.capture([pg for *_, pg in pages])
    except Exception as e:  # noqa: BLE001 — 시작 전 코드가 뜨지 않으면(옛 의존성 등) 견주지 못한 것으로 알린다
        return [], [f"시작 전 코드를 띄우지 못해 고친 기존 화면을 그때 모습과 견주지 못함 ({type(e).__name__}: {str(e)[:120]})"]
    finally:
        if server:
            server.close()
    return list(zip(pages, got)), []


def _route_existed(run: dict, path: str) -> bool:
    """서버 화면 경로가 실행 전 코드에 있었나 — 시작 전 체크포인트의 파일에서 경로의 마지막 조각을 찾는다 ('/' 는 앱이 있으면 있다)."""
    from beom_workbench.core import checkpoints, tasks
    seg = [x for x in path.split("?")[0].split("#")[0].split("/") if x]
    cid = tasks._start_checkpoint(settings, run)
    if not cid:
        return False
    if not seg:
        return True
    keep = [x for x in seg if not re.fullmatch(r"\d+|[0-9a-f-]{16,}", x)]  # /members/3 → /members (id 는 빼고)
    if not keep:
        return False
    needle = "/" + "/".join(keep)  # 경로 모양 그대로 — 'new'·'list' 같은 낱말만으로는 README·SQL 어디에나 걸렸다
    shadow = inbox.run_dir(settings, run["id"]) / "shadow.git"
    try:
        # 경로가 통째로 — '/list' 가 '/members/list' 의 끝에 걸리지 않게 앞뒤가 경로 글자가 아니어야 한다
        pat = r"(^|[^A-Za-z0-9_/.-])" + re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", needle) + r"([^A-Za-z0-9_-]|$)"
        r = checkpoints._git(shadow, Path(run["target_root"]), "grep", "-q", "-E", "-I", pat, cid, "--", ".", check=False)
    except Exception:  # noqa: BLE001
        return False
    return r.returncode == 0


def _changed_files(run: dict) -> list[str]:
    """시작 전 대비 바뀐 파일 — 비교를 못 하면 빈 목록."""
    from beom_workbench.core import tasks
    from beom_workbench.core.checkpoints import Checkpoints
    base = tasks._start_checkpoint(settings, run)
    if not base:
        return []
    try:
        return [f["path"] for f in Checkpoints(inbox.run_dir(settings, run["id"]), run["target_root"]).changes(base).get("files") or []]
    except Exception:  # noqa: BLE001
        return []


def _fix_sig(text: str) -> str:
    """같은 부품에서 오는 fix 를 견주는 모양 — 숫자(크기·대비 값)와 요소 안 글자('VIP 회원 혜택' 같은 칸의 값)는 화면마다 달라 뺀다.
    남는 것: 무슨 문제인지와 어느 부품(선택자)인지. 실험: 기존 회원 상세와 같은 공통 입력칸 테두리가 값이 달라 '새 문제'로 갈렸다."""
    return re.sub(r"\d+(?:\.\d+)?", "#", re.sub(r"'[^']*'", "''", str(text))).strip()


UNREAD = "점검 결과를 읽지 못함"
_COLOR_CAUSE = re.compile(r"^(입력칸 경계가 흐림|포커스 표시가 흐림)[^(]*\((?:테두리|그림자|외곽선|바탕) (#[0-9a-f]{6})", re.I)


def _same_cause(a: str, b: str) -> bool:
    """두 fix 가 같은 원인인가 — 테두리·포커스는 같은 색이면 같은 공통 스타일(칸의 id 가 달라도), 가로 넘침은 넘치게 한 요소의 클래스가
    겹치면 같은 배치. 실험: 공통 '.field input' 테두리가 새 칸에 id(#cp-name)가 있다는 이유로 '새 문제'로 갈렸다."""
    ma, mb = _COLOR_CAUSE.match(a), _COLOR_CAUSE.match(b)
    if ma and mb:
        return ma.group(1) == mb.group(1) and ma.group(2).lower() == mb.group(2).lower()
    if a.startswith("가로로 넘침") and b.startswith("가로로 넘침"):
        return bool(_overflow_classes(a) & _overflow_classes(b))
    return False


def _overflow_classes(fix: str) -> set[str]:
    """'가로로 넘침 — … (main.main, label.field, #cp-name)' → 클래스가 있는 요소만 {'main.main', 'label.field'} (id 는 화면마다 다르다)."""
    groups = re.findall(r"\(([^)]*)\)", fix)
    return {x.strip() for x in groups[-1].split(",") if "." in x} if groups else set()


def split_design_fixes(report: list[dict], cmp_report: list[dict]) -> tuple[list[str], list[str]]:
    """새 화면의 fix 를 기존 화면(compare)에서도 같은 크기로 잰 결과와 견준다 — 같은 것이 기존 화면에도 있으면 기존 제품의 문제,
    아니면 새 화면이 만든 문제. compare 가 없으면 모두 새 화면의 문제로 본다 (기존 문제라는 말만으로는 가르지 않는다)."""
    seen = {(x["size"], _fix_sig(f)) for x in cmp_report for f in x.get("fix") or []}
    olds = [(x["size"], f) for x in cmp_report for f in x.get("fix") or []]
    old, new = [], []
    for x in report:
        for f in x.get("fix") or []:
            if f.startswith(UNREAD):
                continue  # 잴 수 없었던 화면(없는 주소·이동 중) — 디자인 문제가 아니다 (점검 결과에는 그대로 보인다)
            item = f"{x['page']} {x['size']}: {f}"
            same = (x["size"], _fix_sig(f)) in seen or any(sz == x["size"] and _same_cause(f, g) for sz, g in olds)
            (old if same else new).append(item)
    return list(dict.fromkeys(old)), list(dict.fromkeys(new))


from beom_workbench.core import designscope as _ds  # noqa: E402 — 규칙 조립(runner)과 같은 판정
from beom_workbench.core.designscope import design_scope  # noqa: E402



_PRIVATE_HOST = re.compile(r"^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.|0\.0\.0\.0|\[?::1\]?)", re.I)


@mcp.tool()
def wb_fetch_doc(url: str, find: list[str] | None = None) -> dict:
    """공개 문서를 툴킷이 직접 받는다 — 외부 서비스의 공식 규격·개발자 문서·내려받는 가이드(PDF 등)를 확인할 때. 에이전트의 웹 열람이
    막히는 주소도 툴킷은 받을 수 있다 (실험: 한 쇼핑 채널의 상품 피드 가이드 PDF 를 웹 열람이 막아 '확인 못 함'으로 남겼는데 직접 받으면 열렸다).
    HTML 은 글(마크다운)로 돌려주고 전문은 .beom/tmp/web/ 에 둔다. PDF·문서 파일은 그 폴더에 저장하고 경로를 준다 — 편집 도구의
    읽기(Read)로 연다. find=["상품명", "title"] 처럼 찾을 말을 주면 그 말이 나오는 곳을 앞뒤와 함께 먼저 보여 준다.
    바깥에 접속하는 일이라 웹 권한 규칙을 따른다 (묻기면 사용자에게 한 번 묻는다). 이 PC·사내 주소(localhost 등)는 받지 않는다."""
    import urllib.error
    from beom_workbench.core import docs

    run = _run()
    if run is None:
        return {"ok": False, "error": "자동 실행 밖"}
    m = re.match(r"^https?://([^/\s:]+)", str(url or ""))
    if not m or _PRIVATE_HOST.match(m.group(1)):
        return {"ok": False, "error": "공개 http(s) 주소만 받는다"}
    decision = inbox.decide(run, "web")
    if decision == "refuse":
        return {"ok": False, "error": "규칙상 웹 접속이 허용되지 않음 — 그 값은 '확인 못 함'으로 남긴다"}
    if decision == "ask" and WEB_READ not in (run.get("always_allow") or []):
        ans = inbox.ask(settings, run["id"], "permission", {
            "summary": f"공개 문서 받기 (웹 접속 — {WEB_READ_NOTE}): " + url,
            "tool": "mcp:wb_fetch_doc", "input": {"url": url}, "permission_kind": "web"})
        if not ans.get("allow"):
            return {"ok": False, "error": "사용자가 문서 받기를 거절함 — 그 값은 '확인 못 함'으로 남긴다"}
        # 공개 페이지 읽기는 한 번 허락받으면 이 작업 동안 묻지 않는다 (실험 150·151: 조사 중 같은 카드가 22~53번 떴다).
        # 에이전트의 다른 웹 접속(검색·셸)은 그대로 규칙을 따른다 — 넓히는 것은 툴킷이 하는 읽기뿐
        inbox.allow_always(settings, run["id"], WEB_READ)
        if ans.get("always"):
            inbox.allow_always(settings, run["id"], "web")
    try:
        final, ctype, raw = docs._get(url, 30, 25_000_000)
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}" + (" — 로그인이 필요한 문서로 보인다" if e.code in (401, 403) else "")}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return {"ok": False, "error": "받지 못함 — " + str(getattr(e, "reason", e))[:200]}
    folder = Path(run["target_root"]) / ".beom" / "tmp" / "web"
    folder.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w-]+", "_", re.sub(r"^https?://", "", final)).strip("_")[:80] or "doc"
    head = raw[:8]
    ext = ".pdf" if head.startswith(b"%PDF") or "pdf" in ctype else docs._ext_of(final, ctype)
    if ext in (".pdf", ".docx", ".xlsx", ".pptx", ".hwpx") or head.startswith(b"PK"):
        f = folder / f"{stem}{ext if ext.startswith('.') else '.bin'}"
        f.write_bytes(raw)
        inbox.log(settings, run["id"], "web_doc", url=url, doc=f.suffix, bytes=len(raw))
        out = {"ok": True, "url": final, "saved": f.relative_to(Path(run["target_root"])).as_posix(), "kind": f.suffix, "bytes": len(raw)}
        if f.suffix != ".pdf":
            try:
                text = docs.to_text(f) or ""
                out.update(text=text[:6000], **({"found": _find_in(text, find)} if find else {}))
            except Exception:  # noqa: BLE001 — 글을 뽑지 못해도 파일은 남는다
                pass
        else:
            # 툴킷이 글을 뽑는다 — 에이전트의 PDF 읽기는 PC 에 따로 프로그램이 있어야 쪽을 열어 실패했다 (실험 136)
            from beom_workbench.core import pdftext
            pages = pdftext.pages(raw)
            text = "".join(f"\n\n[{i + 1}쪽]\n{t}" for i, t in enumerate(pages) if t.strip()).strip()
            if text:
                t = f.with_suffix(".txt")
                t.write_text(text, encoding="utf-8")
                out.update(pages=len(pages), text_saved=t.relative_to(Path(run["target_root"])).as_posix(), text=text[:6000],
                           **({"found": _find_in(text, find)} if find else {}),
                           next="PDF 의 글을 뽑았다 — 더 볼 곳은 text_saved 를 읽는다. 읽은 값의 출처는 url(과 쪽)로 적는다")
            else:
                out["next"] = ("PDF 에서 글을 뽑지 못했다(그림으로 된 쪽이거나 특수한 형식) — 편집 도구의 읽기(Read)로 saved 를 열어 본다. "
                               "그래도 안 되면 그 값은 '확인 못 함 — 이 PDF'로 남긴다")
        return out
    try:
        body = raw.decode(re.search(r"charset=([\w-]+)", ctype).group(1) if "charset=" in ctype else "utf-8", "replace")
    except LookupError:
        body = raw.decode("utf-8", "replace")
    md, _imgs, title = docs.html_to_md(body, final, whole_page=True) if "<" in body[:2000] else (body, [], final)
    text = re.sub(r"\x00IMG\d+\x00", "", md)
    links = _links_in(body, final)
    rendered = False
    if len(text.strip()) < 400 and "<script" in body:
        # 화면을 스크립트로 그리는 페이지 — 툴킷의 브라우저 엔진으로 띄워 글과 링크를 읽는다 (안내·내려받기 링크가 스크립트 안에 있었다)
        from beom_workbench.core import screens
        if screens.available():
            got = screens.capture([{"url": final, "out": str(folder / f"{stem}.png"), "width": 1280, "height": 900, "wait_ms": 3000,
                                    "dump": True, "timeout_ms": 30000}])
            if got and got[0].get("text"):
                text, links, rendered = got[0]["text"], [(a, h) for a, h in got[0].get("links") or [] if h.startswith("http")], True
    f = folder / f"{stem}.md"
    f.write_text(text, encoding="utf-8")
    inbox.log(settings, run["id"], "web_doc", url=url, doc="html", chars=len(text))
    out = {"ok": True, "url": final, "title": title, "saved": f.relative_to(Path(run["target_root"])).as_posix(), "text": text[:6000],
           "links": _pick_links(links, find), **({"rendered": True} if rendered else {})}
    if find:
        out["found"] = _find_in(text, find)
    if not rendered and len(text.strip()) < 400 and "<script" in body:
        out["note"] = ("글이 거의 없다 — 화면을 스크립트로 그리는 페이지다. 같은 내용의 내려받는 가이드(PDF)·도움말 페이지를 찾아 받는다")
    return out


def _links_in(html: str, base: str) -> list[tuple[str, str]]:
    """페이지의 링크 — (링크 글, 절대 주소). 공식 사이트 안에서 안내·개발자 문서·내려받기 링크를 따라가게 한다."""
    from urllib.parse import urljoin
    out = []
    for m in re.finditer(r"<a\b[^>]*href=[\"']([^\"'#][^\"']*)[\"'][^>]*>(.*?)</a>", html, re.S | re.I):
        href = urljoin(base, m.group(1).strip())
        if href.startswith("http"):
            out.append((re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(2))).strip()[:80], href))
    return list(dict.fromkeys(out))[:400]


def _pick_links(links: list, find: list[str] | None, n: int = 40) -> list[str]:
    """돌려줄 링크 — 찾을 말이 링크 글·주소에 든 것 먼저, 그다음 안내·가이드·개발자·시험·내려받기 같은 링크."""
    words = [str(w).lower() for w in find or []]
    hint = ("guide", "가이드", "developer", "개발", "api", "test", "시험", "테스트", "sandbox", "download", "다운로드", "spec", "규격", "doc")
    def score(item) -> int:
        t = (item[0] + " " + item[1]).lower()
        return (2 if any(w in t for w in words) else 0) + (1 if any(h in t for h in hint) else 0)
    ranked = sorted(links, key=score, reverse=True)
    return [f"{t or '(글 없음)'} — {h}" for t, h in ranked[:n]]


def _find_in(text: str, words: list[str] | None, width: int = 260, per: int = 3) -> dict:
    """찾을 말마다 나오는 곳 몇 군데를 앞뒤와 함께."""
    out = {}
    for w in (words or [])[:8]:
        hits = [m.start() for m in re.finditer(re.escape(str(w)), text)][:per]
        out[str(w)] = [text[max(0, i - width // 2): i + width].replace("\n", " ") for i in hits]
    return out


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
    if decision == "ask" and WEB_READ not in (run.get("always_allow") or []):
        ans = inbox.ask(settings, run["id"], "permission", {
            "summary": f"참고 화면 찍기 (웹 접속 — {WEB_READ_NOTE}): " + ", ".join(picks), "tool": "mcp:wb_design_refs", "input": {"urls": picks},
            "permission_kind": "web"})
        if not ans.get("allow"):
            inbox.log(settings, run["id"], "design_refs_declined", urls=picks)
            return {"ok": False, "error": "사용자가 참고 화면 찍기를 거절함 — 아는 범위에서 참고 서비스를 적는다"}
        inbox.allow_always(settings, run["id"], WEB_READ)
        if ans.get("always"):
            inbox.allow_always(settings, run["id"], "web")
    out_dir = Path(run["target_root"]).resolve() / ".beom" / "refs"
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = []
    for u in picks:
        name = re.sub(r"[^\w-]+", "_", re.sub(r"^https?://", "", u)).strip("_")[:60] + ("-mobile" if mobile else "")
        w, h = (screens.MOBILE[0], screens.MOBILE[1]) if mobile else (screens.DESKTOP[0], 900)  # 점검 폭은 한 곳(screens)에서
        pages.append({"url": u, "out": str(out_dir / f"{name}.png"), "width": w, "height": h, "wait_ms": 2500, "full_page": False,
                      "timeout_ms": 30000})
    try:
        got = screens.capture(pages)
    except ToolError as e:
        # 화면 엔진이 찍지 못했다 — 기록해 두어 계획 확인이 같은 것을 다시 요구하지 않게 (못 하는 일을 요구하면 계획이 영영 막힌다)
        inbox.log(settings, run["id"], "design_refs_failed", error=e.message[:300])
        return {"ok": False, "error": e.message, "note": "참고 화면 없이 진행해도 된다 — 계획은 이것 때문에 막히지 않는다"}
    shots = [{"url": u, "ok": bool(r.get("ok")), "shot": r.get("out") if r.get("ok") else None, "title": r.get("title"),
              "errors": (r.get("errors") or [])[:2]} for u, r in zip(picks, got)]
    inbox.log(settings, run["id"], "design_refs", shots=[s["shot"] for s in shots if s["shot"]], urls=[s["url"] for s in shots if s["ok"]])
    return {"ok": True, "shots": shots}


@mcp.tool()
def wb_browser_capture(urls: list[str], mobile: bool = False) -> dict:
    """사용자가 앱 브라우저에서 로그인하고 '에이전트가 화면을 찍어도 됨'을 허락한 사이트(지시의 [로그인 허락된 사이트])의 화면을 그
    로그인 상태로 찍는다 (최대 8개). 개발·스테이징 관리자 화면처럼 로그인해야 보이는 실제 화면을 근거로 쓸 때.
    먼저 소스에서 메뉴·라우트 설정(메뉴 파일·라우터·컨트롤러 경로)을 찾아 이 작업과 관련된 메뉴의 주소를 고른 뒤 그 주소만 찍는다
    — 첫 화면부터 눌러 다니며 찾지 않는다. 결과의 shot 을 열어 보고, links(그 화면의 메뉴·링크)로 다음에 볼 주소를 정한다.
    허락되지 않은 사이트면 찍지 않는다 — 사용자에게 오른쪽 '브라우저'에서 그 사이트에 로그인하고 허락해 달라고 wb_person_ask 로 묻는다.
    비밀번호·쿠키는 에이전트에게 오지 않는다."""
    from beom_workbench.common.response import ToolError
    from beom_workbench.core import browser_auth, screens

    run = _run()
    if run is None:
        return {"ok": False, "error": "자동 실행 밖"}
    picks = [str(u).strip() for u in (urls or [])[:8] if re.match(r"^https?://[^\s/]+", str(u).strip())]
    if not picks:
        return {"ok": False, "error": "urls 에 http(s) 주소를 1~8개"}
    if not screens.available():
        return {"ok": False, "error": "이 설치에는 화면 엔진(PySide6)이 없음"}
    allowed = [h["host"] for h in browser_auth.hosts(settings)]
    blocked = [u for u in picks if browser_auth.cookies_for(settings, u) is None]
    if blocked:
        return {"ok": False, "error": "로그인 허락된 사이트가 아님: " + ", ".join(blocked[:3]),
                "allowed_hosts": allowed,
                "next": "사용자에게 묻는다(wb_person_ask): 앱 오른쪽 '브라우저'에서 그 사이트에 로그인한 뒤 '에이전트가 이 사이트 화면을 찍어도 됨'을 "
                        "눌러 달라고. 허락 전에는 소스(화면 파일·공통 부품)로 구조를 따른다"}
    sub = "brief/shots" if run.get("mode") == "plan" else "refs/live"
    out_dir = Path(run["target_root"]).resolve() / ".beom" / sub
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = []
    for u in picks:
        name = re.sub(r"[^\w-]+", "_", re.sub(r"^https?://", "", u)).strip("_")[:70] + ("-mobile" if mobile else "")
        w, h = (screens.MOBILE[0], screens.MOBILE[1]) if mobile else (screens.DESKTOP[0], 900)
        pages.append({"url": u, "out": str(out_dir / f"{name}.png"), "width": w, "height": h, "wait_ms": 2500, "full_page": False,
                      "timeout_ms": 30000, "dump": True, "cookies": browser_auth.cookies_for(settings, u)})
    try:
        got = screens.capture(pages)
    except ToolError as e:
        return {"ok": False, "error": e.message}
    shots = []
    for u, r in zip(picks, got):
        text = str(r.get("text") or "")
        login_page = bool(r.get("pw"))  # 그 화면에 비밀번호 칸이 있다 — 로그인 화면으로 돌아갔다
        shots.append({"url": u, "ok": bool(r.get("ok")), "shot": r.get("out") if r.get("ok") else None, "title": r.get("title"),
                      "text": text[:1500], "links": (r.get("links") or [])[:60], "errors": (r.get("errors") or [])[:2],
                      **({"note": "로그인 화면이 열렸다 — 허락한 로그인이 끝났을 수 있다. 사용자에게 다시 로그인해 달라고 묻는다"} if login_page else {})})
    inbox.log(settings, run["id"], "browser_capture", urls=[s["url"] for s in shots if s["ok"]], shots=[s["shot"] for s in shots if s["shot"]])
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
