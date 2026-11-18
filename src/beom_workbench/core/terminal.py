"""CLI 로 돌릴 때 터미널에서 답하기 — 진행 상황 한 줄, 권한 요청·질문·시작 전 확인·라이선스 확인·인수인계 수락.

앱의 카드와 같은 내용을 터미널에 보여 주고, 앱이 열려 있으면 어느 쪽에서 먼저 답하든 된다. 사람의 결정이라
실제 터미널(표준 입력·출력이 TTY)에서만 쓴다 — 에이전트가 파이프로 CLI 를 부르면 켜지지 않는다.
모든 출력은 표준 오류로 (표준 출력의 JSON 결과를 스크립트가 그대로 읽을 수 있게).
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Callable

from beom_workbench.common.response import ToolError
from beom_workbench.config import Settings
from beom_workbench.core import approvals, inbox

Reader = Callable[[str], str]
Writer = Callable[[str], None]


def _stderr(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def _readline(prompt: str) -> str:
    sys.stderr.write(prompt)
    sys.stderr.flush()
    return sys.stdin.readline().rstrip("\r\n")


class Attendant:
    """실행 하나를 지켜보다가 사람이 할 일이 생기면 터미널에서 묻는다."""

    def __init__(self, settings: Settings, run_id: str, *, read: Reader = _readline, write: Writer = _stderr,
                 tr: Callable[[str], str] = lambda s: s, poll: float = 1.0):
        self.settings, self.run_id = settings, run_id
        self.read, self.write, self.t, self.poll = read, write, tr, poll
        self.stop = threading.Event()
        self._seen_asks: set[str] = set()
        self._seen_gate: str | None = None
        self._last_line: tuple | None = None
        self._deferred = False

    # ---------------------------------------------------------------- 진행
    def start(self) -> threading.Thread:
        th = threading.Thread(target=self.loop, daemon=True)
        th.start()
        return th

    def loop(self) -> None:
        while not self.stop.is_set():
            try:
                self.tick()
            except ToolError as e:
                self.write(self.t(e.message))
            except Exception:  # noqa: BLE001 — 지켜보는 쪽이 실행을 넘어뜨리지 않게
                pass
            self.stop.wait(self.poll)

    def tick(self) -> None:
        from beom_workbench.app import view
        d = view.detail(self.settings, self.run_id)
        run = d["run"]
        act = (d.get("activity") or {}).get("text") if isinstance(d.get("activity"), dict) else None
        if d.get("tone") == "turn":
            act = None  # 앱용 문구('아래 카드를 보고…') 대신 바로 이어서 터미널 질문이 나온다
        line = (run.get("round"), d.get("status"), act)
        if line != self._last_line:
            self._last_line = line
            self.write(self.t(f"· {d.get('status')} · 라운드 {run.get('round') or 0}") + (f" — {self.t(act)}" if act else ""))
        for a in d.get("asks") or []:
            if a.get("id") not in self._seen_asks:
                self._seen_asks.add(a.get("id"))
                self._ask(a)
        # 승인 카드는 라운드가 끝나 실행기가 기다릴 때만 (앱과 같은 규칙) — 도중에 띄우면 기준·케이스·계획을 따로따로 묻게 된다
        from beom_workbench.core import runner
        alive = runner.lock_owner(self.settings, self.run_id) is not None
        gate = run.get("status") in ("waiting_approval", "waiting_plan") or not alive
        reviews = d.get("reviews") or {}
        plan = d.get("plan") or {}
        plan_open = bool(plan.get("steps")) and not plan.get("approved") and run.get("status") != "done"
        sig = f"{d.get('review_sig')}|{d.get('plan_sig') if plan_open else ''}"
        if gate and (reviews.get("count") or plan_open) and sig != self._seen_gate and not self._deferred:
            self._seen_gate = sig
            self._approve(d, reviews, plan if plan_open else None)
        lic = d.get("license") or {}
        if lic.get("warning") and lic.get("acked") is False and run.get("status") == "waiting_approval" \
                and self._seen_gate != "license:" + lic["warning"]:
            self._seen_gate = "license:" + lic["warning"]
            self._license(lic["warning"])

    # ---------------------------------------------------------------- 묻기
    def _answered_elsewhere(self, rid: str) -> bool:
        d = inbox.run_dir(self.settings, self.run_id) / "inbox"
        return not (d / f"{rid}.json").exists() or (d / f"{rid}.answer.json").exists()

    def _ask(self, a: dict) -> None:
        rid = a["id"]
        if a.get("kind") == "permission":
            self.write("\n" + self.t("권한 요청") + " — " + self.t(str(a.get("summary") or "")))
            if a.get("rule"):
                self.write("  " + self.t("[c] 이 명령은 항상") + f": {a['rule']} …")
            raw = self.read(self.t("허용할까요? [y] 허용  [a] 이 프로젝트에서 항상  [n] 거절 > ")).strip().lower()
            data = {"allow": raw in ("y", "yes", "a", "ㅛ", "ㅁ", "c", "ㅊ"), "always": raw in ("a", "ㅁ"),
                    **({"remember": True} if raw in ("c", "ㅊ") and a.get("rule") else {})}
        else:
            self.write("\n" + self.t("질문") + " — " + self.t(str(a.get("question") or "")))
            choices = a.get("choices") or []
            for i, c in enumerate(choices, 1):
                self.write(f"  {i}. {self.t(str(c))}")
            raw = self.read(self.t("번호를 고르거나 직접 답을 적어 주세요 > ") if choices else self.t("답을 적어 주세요 > ")).strip()
            if raw.isdigit() and 1 <= int(raw) <= len(choices):
                data = {"choice": choices[int(raw) - 1]}
            elif raw:
                data = {"text": raw}
            elif choices:
                data = {"choice": choices[0]}
            else:
                data = {"text": "추천대로 해 주세요"}
        if self._answered_elsewhere(rid):
            self.write(self.t("이미 다른 곳(앱)에서 답했습니다"))
            return
        approvals.answer(self.settings, self.run_id, rid, data)

    def _approve(self, d: dict, reviews: dict, plan: dict | None) -> None:
        w, t = self.write, self.t
        reqs = reviews.get("requirements") or []
        exp = reviews.get("expectations") or []
        # 앱과 같은 제목 — 계획과 함께 받는 첫 확인은 '시작 전 확인'(끝에 한 번이면 '마지막 확인'), 그 뒤에 다시 받는 확인은 무엇을 받는지로
        if plan:
            title = "마지막 확인" if (d.get("run") or {}).get("confirm_at") == "end" else "시작 전 확인"
        else:
            title = "요구사항과 합격 기준" if (reqs or exp) else "확인할 항목"
        w("\n" + t(title))
        if plan and plan.get("summary"):
            w("  " + t(str(plan["summary"])))
        if reqs:
            w(t("만들 것") + f" ({len(reqs)})")
            for r in reqs[:15]:
                w(f"  {r.get('id', '')} {t(str(r.get('text') or ''))}")
            if len(reqs) > 15:
                w(f"  … +{len(reqs) - 15}")
        if exp:
            w(t("합격 기준 {0}개").replace("{0}", str(len(exp))) + " — " + t("이것이 모두 되면 완료"))
            if not plan:  # 나중에 다시 받는 확인은 무엇이 더해지거나 바뀌었는지가 요점 — 하나씩 보여 준다
                for e in exp[:15]:
                    w(f"  - {t(str(e.get('description') or e.get('case') or ''))}")
                if len(exp) > 15:
                    w(f"  … +{len(exp) - 15}")
        for x in [c for c in (reviews.get("case_changes") or []) if isinstance(c, dict)][:5]:
            if x.get("removed"):
                w("  " + t("빠짐") + f": {', '.join(x['removed'])} — " + t("이전 승인 기준에서 빠진 케이스"))
            if x.get("changed"):
                w("  " + t("바뀜") + f": {', '.join(x['changed'])} — " + t("기대값이 이전 승인과 다름"))
        for x in (reviews.get("divergences") or [])[:10]:  # 사람이 확인할 차이 허용
            w("  " + t("차이 허용") + f": {t(str(x.get('summary') or ''))}")
        for x in (reviews.get("checks") or [])[:5]:  # 툴킷이 재지 못해 사람이 확인할 것
            w("  " + t("직접 확인할 점") + f": {t(str(x.get('summary') or ''))}")
        for x in (reviews.get("omitted") or [])[:10]:  # 옮기지 않는 것
            w("  " + t("생략") + f": {t(str(x.get('summary') or ''))}")
        if plan:
            if plan.get("decisions"):
                w(t("정할 것") + " — " + t("추천이 골라져 있습니다 — 바꾸려면 앱에서 누르세요"))
                for dd in plan["decisions"]:
                    w(f"  · {t(str(dd.get('topic')))}: {t(str(dd.get('choice')))}")
            w(t("진행 순서"))
            for i, s in enumerate(plan.get("steps") or [], 1):
                w(f"  {i}. {t(str(s.get('title') or ''))}")
        raw = self.read(t("승인할까요? [y] 승인  [Enter] 나중에(앱에서)  — 고칠 점을 적으면 반영해 다시 보여 줍니다 > ")).strip()
        if raw.lower() in ("y", "yes", "ㅛ"):
            try:
                if reviews.get("count"):
                    approvals.approve_reviews(self.settings, self.run_id, d["review_sig"], channel="cli-tty")
                if plan:
                    approvals.approve_plan(self.settings, self.run_id, d["plan_sig"], channel="cli-tty")
                w(t("승인했습니다 — 이어서 진행합니다"))
            except ToolError as e:
                w(t(e.message))
                self._seen_gate = None  # 바뀐 내용을 다시 보여 준다
        elif raw:
            approvals.send_message(self.settings, self.run_id, raw)
            w(t("보냈습니다 — 반영해 다시 확인을 요청합니다"))
        else:
            self._deferred = True  # 이번에는 앱에서 — 같은 카드를 다시 묻지 않는다
            w(t("앱에서 확인해 주세요"))

    def _license(self, warning: str) -> None:
        self.write("\n" + self.t("원본 라이선스 확인") + " — " + self.t(warning))
        raw = self.read(self.t("이 코드를 써도 되는지 확인했나요? [y] 확인함  [Enter] 나중에 > ")).strip().lower()
        if raw in ("y", "yes", "ㅛ"):
            approvals.ack_license(self.settings, self.run_id, warning, channel="cli-tty")

    def finish(self) -> None:
        """끝났으면 인수인계를 보여 주고 수락을 묻는다."""
        from beom_workbench.app import view
        self.stop.set()
        d = view.detail(self.settings, self.run_id)
        run, h = d["run"], d.get("handoff") or {}
        if run.get("status") != "done" or run.get("accepted") or not h:
            return
        self.write("\n" + self.t("인수인계") + " — " + self.t(str(h.get("summary") or "")))
        for step in h.get("how_to_run") or []:
            self.write("  " + self.t(str(step)))
        raw = self.read(self.t("수락할까요? [y] 수락  — 고칠 점을 적으면 이어서 고칩니다 > ")).strip()
        if raw.lower() in ("y", "yes", "ㅛ"):
            approvals.accept(self.settings, self.run_id)
            self.write(self.t("수락했습니다"))
        elif raw:
            approvals.send_message(self.settings, self.run_id, raw)
            self.write(self.t("보냈습니다 — beom-workbench run --resume 으로 이어서 진행합니다"))


def interactive() -> bool:
    """사람이 직접 터미널에서 돌리는가."""
    try:
        return sys.stdin.isatty() and sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def wait_answered(settings: Settings, run_id: str, rid: str, timeout: float = 5) -> bool:
    end = time.monotonic() + timeout
    d = inbox.run_dir(settings, run_id) / "inbox"
    while time.monotonic() < end:
        if (d / f"{rid}.answer.json").exists() or not (d / f"{rid}.json").exists():
            return True
        time.sleep(0.05)
    return False
