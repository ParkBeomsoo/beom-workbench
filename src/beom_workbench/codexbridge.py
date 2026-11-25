"""Codex through its app-server — so Codex can ask, and the person answers, instead of the toolkit guessing.

`codex exec` cannot stop to ask anyone: with approvals off, whatever Codex's own sandbox blocks simply fails, and the toolkit
had to guess ahead of time which commands would (and offered a side door, wb_run). The app-server is Codex's official
interface for apps: Codex works inside its sandbox and, when it needs more (run a command outside it, write a folder,
reach the network), it sends a request the app answers — accept, accept for the session, or decline.

This bridge runs one turn on the app-server and:
- answers each request through the toolkit's single permission decision (`modules.run.approve_action` — the same one
  Claude's permission prompts use), which shows an app card when the run's rules say to ask;
- prints the same JSON events `codex exec --json` prints, so the runner reads Codex the same way as before.

usage: python -m beom_workbench.codexbridge <spec.json>   (the prompt on stdin)
spec: {"codex": [exe, ...], "config": ["key=value", ...], "cwd", "home", "run_id", "model"?, "effort"?, "session_id"?,
       "network": bool}
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

_OUT = threading.Lock()


def emit(ev: dict) -> None:
    """runner 가 읽는 이벤트 한 줄 (codex exec --json 과 같은 모양)."""
    with _OUT:
        sys.stdout.write(json.dumps(ev, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def note(text: str) -> None:
    sys.stderr.write(f"[codexbridge] {text}\n")
    sys.stderr.flush()


class Server:
    """codex app-server 와 줄 단위 JSON-RPC (머리글 jsonrpc 없음)."""

    def __init__(self, argv: list[str], cwd: str, env: dict) -> None:
        kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, **kw)
        self._id = 0
        self._wait: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self.inbox: queue.Queue = queue.Queue()  # 알림·서버 요청
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for raw in self.proc.stdout:
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if "id" in msg and "method" not in msg:  # 우리 요청의 답
                q = self._wait.pop(msg["id"], None)
                if q:
                    q.put(msg)
            else:
                self.inbox.put(msg)
        self.inbox.put(None)  # 서버가 끝났다

    def send(self, msg: dict) -> None:
        with self._lock:
            self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            self.proc.stdin.flush()

    def request(self, method: str, params: dict, timeout: float = 120) -> dict:
        with self._lock:
            self._id += 1
            rid = self._id
        q: queue.Queue = queue.Queue()
        self._wait[rid] = q
        self.send({"id": rid, "method": method, "params": params})
        msg = q.get(timeout=timeout)
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error'].get('message') if isinstance(msg['error'], dict) else msg['error']}")
        return msg.get("result") or {}

    def notify(self, method: str, params: dict | None = None) -> None:
        self.send({"method": method, **({"params": params} if params is not None else {})})

    def reply(self, rid, result: dict | None = None, error: str | None = None) -> None:
        self.send({"id": rid, **({"error": {"code": -32601, "message": error}} if error else {"result": result or {}})})

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# ---------------------------------------------------------------- 승인 요청 → 툴킷의 한 가지 판단

def _decide(tool: str, inp: dict, escalation: bool = False) -> tuple[bool, str]:
    from beom_workbench.modules import run as run_tools
    out = json.loads(run_tools.approve_action(tool, inp, escalation=escalation))
    return out.get("behavior") == "allow", str(out.get("message") or "")


SHELL = "PowerShell" if os.name == "nt" else "Bash"


class Turn:
    def __init__(self, srv: Server, spec: dict) -> None:
        self.srv, self.spec = srv, spec
        self.thread_id = ""
        self.turn_id = ""
        self.files: dict[str, list[str]] = {}  # fileChange item → 바꿀 파일들 (승인 요청에는 경로가 없다)
        self.usage_start: dict | None = None
        self.usage_now: dict | None = None
        self.steers: list[str] = []

    def handle_request(self, msg: dict) -> None:
        method, params, rid = msg.get("method"), msg.get("params") or {}, msg.get("id")
        try:
            if method == "item/commandExecution/requestApproval":
                ctx = params.get("networkApprovalContext")
                if ctx:  # 막힌 네트워크 접속 — 웹 접속 규칙
                    ok, why = _decide("WebFetch", {"url": f"{ctx.get('protocol') or 'https'}://{ctx.get('host')}"})
                else:
                    ok, why = _decide(SHELL, {"command": str(params.get("command") or "")}, escalation=True)
                self.srv.reply(rid, {"decision": "accept" if ok else "decline"})
                if not ok and why:
                    self.steer(why)
            elif method == "item/fileChange/requestApproval":
                paths = self.files.get(params.get("itemId") or "", [])
                if params.get("grantRoot"):
                    paths = paths or [str(Path(params["grantRoot"]) / "_")]
                results = [_decide("Edit", {"file_path": p}) for p in paths] or [(False, "바꿀 파일을 알 수 없음")]
                ok = all(r[0] for r in results)
                self.srv.reply(rid, {"decision": "accept" if ok else "decline"})
                why = next((r[1] for r in results if not r[0] and r[1]), "")
                if not ok and why:
                    self.steer(why)
            elif method == "item/permissions/requestApproval":
                self.srv.reply(rid, self.permissions(params))
            elif method == "item/tool/requestUserInput":
                self.srv.reply(rid, {"answers": self.ask_user(params)})
            elif method == "mcpServer/elicitation/request":
                self.srv.reply(rid, {"action": "decline"})  # 우리 도구는 미리 허용 — 다른 서버는 이 실행에서 꺼 두었다
            else:
                self.srv.reply(rid, error=f"이 앱은 {method} 를 처리하지 않음")
        except Exception as e:  # noqa: BLE001 — 답을 못 하면 Codex 가 멈춘다: 거절로 답한다
            note(f"{method} 처리 실패: {type(e).__name__}: {e}")
            if method == "item/permissions/requestApproval":
                self.srv.reply(rid, {"permissions": {}, "scope": "turn"})
            elif method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
                self.srv.reply(rid, {"decision": "decline"})
            else:
                self.srv.reply(rid, error=str(e)[:200])

    def permissions(self, params: dict) -> dict:
        """폴더 쓰기·읽기·네트워크 권한 요청 — 대상 폴더 안은 이미 쓸 수 있다. 밖은 규칙대로 (카드). 허락한 것만 돌려준다."""
        want = params.get("permissions") or {}
        fs, net = want.get("fileSystem") or {}, want.get("network") or {}
        root = Path(self.spec["cwd"]).resolve()
        granted: dict = {}
        writes = []
        for p in fs.get("write") or []:
            path = str(p.get("path") if isinstance(p, dict) else p)
            inside = Path(path).resolve().is_relative_to(root) if path else False
            ok, _ = (True, "") if inside else _decide("Write", {"file_path": str(Path(path) / "_")}, escalation=True)
            if ok:
                writes.append(p)
        reads = []
        for p in fs.get("read") or []:
            path = str(p.get("path") if isinstance(p, dict) else p)
            ok, _ = _decide("Read", {"file_path": path})
            if ok:
                reads.append(p)
        if writes or reads:
            granted["fileSystem"] = {**({"write": writes} if writes else {}), **({"read": reads} if reads else {})}
        if net.get("enabled"):
            ok, _ = _decide("WebFetch", {"url": "https://(network)"})
            if ok:
                granted["network"] = {"enabled": True}
        return {"permissions": granted, "scope": "turn"}

    def ask_user(self, params: dict) -> dict:
        from beom_workbench.config import load_settings
        from beom_workbench.core import inbox
        settings, out = load_settings(), {}
        for q in params.get("questions") or []:
            opts = [o.get("label") if isinstance(o, dict) else str(o) for o in q.get("options") or []]
            ans = inbox.ask(settings, self.spec["run_id"], "question", {"question": q.get("question") or q.get("header") or "",
                                                                       "choices": [o for o in opts if o]})
            out[q.get("id") or ""] = {"answers": [str(ans.get("choice") or ans.get("text") or "")]}
        return out

    def steer(self, text: str) -> None:
        """거절한 까닭을 진행 중인 턴에 알린다 — 승인 응답에는 글을 실을 수 없다 (예: '테스트는 wb_task 로')."""
        if not (self.thread_id and self.turn_id):
            return
        try:
            self.srv.request("turn/steer", {"threadId": self.thread_id, "expectedTurnId": self.turn_id,
                                            "input": [{"type": "text", "text": f"[툴킷] {text}", "text_elements": []}]}, timeout=30)
        except Exception as e:  # noqa: BLE001
            note(f"turn/steer 실패: {e}")

    # ------------------------------------------------------------ 알림 → codex exec --json 모양

    def on_notification(self, msg: dict) -> bool:
        """처리하고, 턴이 끝났으면 True."""
        m, p = msg.get("method"), msg.get("params") or {}
        if m == "item/started":
            item = p.get("item") or {}
            t = item.get("type")
            if t == "commandExecution":
                emit({"type": "item.started", "item": {"type": "command_execution", "command": item.get("command")}})
            elif t == "fileChange":
                self.files[item.get("id") or ""] = [c.get("path") for c in item.get("changes") or [] if c.get("path")]
        elif m == "item/completed":
            item = p.get("item") or {}
            t = item.get("type")
            if t == "agentMessage" and item.get("text"):
                emit({"type": "item.completed", "item": {"type": "agent_message", "text": item["text"]}})
            elif t == "mcpToolCall":
                emit({"type": "item.completed", "item": {"type": "mcp_tool_call", "server": item.get("server"), "tool": item.get("tool"),
                                                         "arguments": item.get("arguments"), "error": item.get("error"),
                                                         "status": "failed" if item.get("status") == "failed" else "completed"}})
            elif t == "fileChange":
                emit({"type": "item.completed", "item": {"type": "file_change", "changes": [
                    {"path": c.get("path"), "kind": (c.get("kind") or {}).get("type")} for c in item.get("changes") or []]}})
        elif m == "thread/tokenUsage/updated":
            tu = p.get("tokenUsage") or {}
            total = tu.get("total") or {}
            if self.usage_start is None:
                self.usage_start = {k: 0 for k in total}
            self.usage_now = total
            last = tu.get("last") or {}
            if last.get("inputTokens"):  # 마지막 호출의 입력 = 지금 대화의 크기 (실행기가 길이를 본다)
                emit({"type": "context.updated", "tokens": int(last.get("inputTokens") or 0),
                      "window": int(tu.get("modelContextWindow") or 0)})
        elif m == "error":
            err = p.get("error") or {}
            if not p.get("willRetry"):
                note(f"오류: {err.get('message')}")
        elif m == "turn/completed":
            turn = p.get("turn") or {}
            if turn.get("status") == "failed" or turn.get("error"):
                emit({"type": "turn.failed", "error": {"message": (turn.get("error") or {}).get("message") or "Codex 턴 실패"}})
            else:
                emit({"type": "turn.completed", "usage": self.usage()})
            return True
        return False

    def usage(self) -> dict:
        now, start = self.usage_now or {}, self.usage_start or {}
        d = {k: max(0, int(now.get(k) or 0) - int(start.get(k) or 0)) for k in now}
        return {"input_tokens": d.get("inputTokens", 0), "cached_input_tokens": d.get("cachedInputTokens", 0),
                "output_tokens": d.get("outputTokens", 0) + d.get("reasoningOutputTokens", 0)}


def main(argv: list[str]) -> int:
    spec = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    # 승인 판단은 툴킷의 도구 서버와 같은 실행 기록을 본다
    os.environ["BEOM_WORKBENCH_HOME"] = spec["home"]
    os.environ["BEOM_WORKBENCH_RUN"] = spec["run_id"]
    prompt = sys.stdin.buffer.read().decode("utf-8")
    args = [*spec["codex"], "app-server"]
    for c in spec.get("config") or []:
        args += ["-c", c]
    env = {k: v for k, v in os.environ.items() if k != "BEOM_WORKBENCH_RUN"}
    srv = Server(args, spec["cwd"], env)
    turn = Turn(srv, spec)
    try:
        srv.request("initialize", {"clientInfo": {"name": "beom-workbench", "title": "beom-workbench", "version": "1"},
                                   "capabilities": {"experimentalApi": False}}, timeout=120)
        srv.notify("initialized")
        common = {"cwd": spec["cwd"], "approvalPolicy": "on-request", "sandbox": "workspace-write",
                  **({"model": spec["model"]} if spec.get("model") else {})}
        if spec.get("session_id"):
            try:  # 예전 판이 보관해 둔 스레드면 꺼낸다 (지금은 라운드마다 툴킷 폴더로 옮겼다가 되돌린다 — agentrecords)
                srv.request("thread/unarchive", {"threadId": spec["session_id"]}, timeout=30)
            except Exception:  # noqa: BLE001 — 보관하지 않은 스레드면 그대로 이어 받는다
                pass
            res = srv.request("thread/resume", {"threadId": spec["session_id"], **common}, timeout=120)
        else:
            res = srv.request("thread/start", common, timeout=120)
        turn.thread_id = ((res.get("thread") or {}).get("id")) or spec.get("session_id") or ""
        if not spec.get("session_id") and spec.get("title"):
            try:  # 목록에 보이는 동안은 툴킷이 연 스레드라는 것을 알 수 있게
                srv.request("thread/name/set", {"threadId": turn.thread_id, "name": spec["title"][:80]}, timeout=30)
            except Exception:  # noqa: BLE001
                pass
        emit({"type": "thread.started", "thread_id": turn.thread_id})
        started = srv.request("turn/start", {"threadId": turn.thread_id,
                                             "input": [{"type": "text", "text": prompt, "text_elements": []}],
                                             **({"effort": spec["effort"]} if spec.get("effort") else {})}, timeout=120)
        turn.turn_id = (started.get("turn") or {}).get("id") or ""
        while True:
            msg = srv.inbox.get()
            if msg is None:
                emit({"type": "turn.failed", "error": {"message": "Codex app-server 가 턴 도중에 끝났습니다"}})
                return 1
            if "id" in msg and "method" in msg:  # 서버의 요청 — 사람 답을 기다릴 수 있어 따로
                threading.Thread(target=turn.handle_request, args=(msg,), daemon=True).start()
                continue
            if msg.get("method") == "turn/started" and not turn.turn_id:
                turn.turn_id = ((msg.get("params") or {}).get("turn") or {}).get("id") or ""
            if turn.on_notification(msg):
                return 0
    except Exception as e:  # noqa: BLE001
        emit({"type": "turn.failed", "error": {"message": f"Codex 연결 실패: {type(e).__name__}: {e}"[:500]}})
        return 1
    finally:
        srv.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
