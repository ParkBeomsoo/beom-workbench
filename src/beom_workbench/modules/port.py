"""포팅 도구 — 분석한 기능을 내 저장소로 옮기거나 다른 언어로 다시 만들고, 원본과 같은 동작인지 검증."""
from __future__ import annotations

import os
from pathlib import Path

from mcp.server.mcpserver import Context
from pydantic import BaseModel, Field

from beom_workbench.common.response import Envelope, ToolError, enveloped
from beom_workbench.core import inbox, port, port_guard, project
from beom_workbench.server import mcp, settings


class _Approval(BaseModel):
    approve: bool = Field(description="위 항목을 모두 승인")


@mcp.tool()
@enveloped("port.slice")
def wb_port_slice(project_id: str, targets: list[str], within: list[str] | None = None, depth: int = 10) -> dict:
    """옮길 범위: 진입점("POST /api/v1/orders") 또는 심볼에서 앞으로 닫힘 — 호출·인터페이스 디스패치·등록(+import 배선)·
    모듈 상태·설정 키·외부 import/호출(대상 언어에서 대체할 것)·명세 역할 테스트·파일별 라이선스·열린 미확인."""
    conn, proj = project.load(settings, project_id)
    return port.slice_(conn, proj, targets, depth=depth, within=within)


def _own_port(project_id: str, port_id: str | None) -> None:
    """자동 실행 중이면 그 실행의 원장만 — 다른 실행의 원장(앞 실행·다른 폴더)에 쓰거나 판정을 받지 않는다 (실험 149: 에이전트가 새로
    만든 원장에 요구사항을 쓰고, 툴킷은 실행의 원장으로 계획을 보아 서로 어긋났다)."""
    rid = os.environ.get("BEOM_WORKBENCH_RUN")
    if not rid or not port_id:
        return
    try:
        run = inbox.load_run(settings, rid)
    except (ToolError, OSError, KeyError):
        return
    if run.get("port_id") and (port_id != run["port_id"] or (project_id and project_id != run.get("project_id"))):
        raise ToolError("invalid_request", f"이 실행의 원장은 project_id={run.get('project_id')} port_id={run['port_id']} 이다 — "
                        "다른 원장은 쓰지 않는다 (새 원장을 만들지 않고 이 원장의 요구사항·케이스를 고친다)",
                        recovery=f"project_id={run.get('project_id')}, port_id={run['port_id']} 로 다시 부른다")


def _link_run(project_id: str, port_id: str | None) -> None:
    """자동 실행 중에 에이전트가 만든 원장을 그 실행에 잇는다 — 레퍼런스 모드에서 원본 분석이 안 돼 요청사항 원장으로 시작해도
    실행기가 그 원장의 승인 대기·완료 조건을 본다 (잇지 않으면 승인 대기로 멈추지 못하고 헛돈다)."""
    rid = os.environ.get("BEOM_WORKBENCH_RUN")
    if not rid or not port_id:
        return

    def put(r):
        if r.get("port_id"):
            return
        if r.get("project_id") and r["project_id"] != project_id:
            r.setdefault("source_project_id", r["project_id"])
        r["project_id"], r["port_id"] = project_id, port_id

    try:
        inbox.update_run(settings, rid, put)
    except (ToolError, OSError, KeyError):
        pass


@mcp.tool()
@enveloped("port.create")
def wb_port_create(project_id: str, name: str, targets: list[str], target_language: str, target_root: str,
                    author: str = "agent", within: list[str] | None = None, depth: int = 10) -> dict:
    """포팅 원장 생성 — 슬라이스 항목(심볼·상태·배선)마다 pending. target_root 는 내 저장소 경로."""
    conn, proj = project.load(settings, project_id)
    out = port.create(conn, proj, name=name, targets=targets, target_language=target_language,
                      target_root=target_root, author=author, within=within, depth=depth)
    _link_run(project_id, out.get("port_id"))
    return out


@mcp.tool()
@enveloped("port.map")
def wb_port_map(project_id: str, port_id: str, updates: list[dict] | None = None, follow: bool = False,
                 actor: str = "agent") -> dict:
    """항목 매핑. updates: [{item, status: ported|omitted|replaced, target: "내 저장소 경로:줄"(ported), note(omitted·replaced)}].
    ported 의 target 은 실제로 있는지와 줄 내용을 기록하고, 이후 줄이 밀리거나 바뀌면 게이트에서 잡는다.
    follow=True: 이동한 대상(같은 내용이 한 곳에만 있음)을 새 줄로 갱신."""
    _own_port(project_id, port_id)
    conn, _ = project.load(settings, project_id)
    if follow:
        return port.follow(conn, port_id, actor)
    return port.map_items(conn, port_id, updates or [], actor)


@mcp.tool()
@enveloped("port.accept")
def wb_port_accept(project_id: str, port_id: str, cases: list[str], reason: str, actor: str = "agent") -> dict:
    """의도적인 동작 차이 허용 '제안' (케이스 이름 패턴, * 가능 — 예: "*~json.items.0.price=소수문자열"). 사유 필수.
    제안만 기록된다 — 사용자가 앱의 승인 카드(앱 밖이면 터미널 port-approve)에서 승인해야 게이트가 통과한다. 에이전트는 승인하지 않는다."""
    _own_port(project_id, port_id)
    conn, _ = project.load(settings, project_id)
    return port.accept(conn, port_id, cases, reason, actor)


@mcp.tool()
@enveloped("make.create")
def wb_make_create(name: str, request: str, target_root: str, context: str | None = None, author: str = "agent") -> dict:
    """요청사항 기반 만들기 시작 — 원본 없이 "이런 기능을 만들어 줘" 에서 출발하는 원장을 만든다.
    target_root: 만들 곳. context: 넣을 저장소·참고 저장소를 wb_repo_open·inventory 한 프로젝트 id (기존 구조에 맞추기).
    이후: wb_make_requirements → 요구사항마다 합격 케이스(expect + req) → wb_port_capture → wb_plan_set → 사용자 승인 → 구현 →
    wb_port_map(요구사항 → 구현 위치) → wb_port_verify → 완료 (자동 실행은 wb_handoff — 판정은 툴킷이, 그 밖에는 wb_port_finish).
    각 응답의 next_step 을 따른다."""
    from beom_workbench.core import make

    out = make.create(settings, name=name, request=request, target_root=target_root, context=context, author=author)
    _link_run(out.get("project_id"), out.get("port_id"))
    return out


@mcp.tool()
@enveloped("make.requirements")
def wb_make_requirements(project_id: str, port_id: str, requirements: list[dict] | None = None, actor: str = "agent") -> dict:
    """요구사항 목록 보기(requirements 생략) 또는 통째로 정하기: [{id?: "R1", text: "확인 가능한 한 문장", from, …출처 칸}].
    사용자 요청을 빠짐없이 나누고, 각 문장은 케이스로 확인할 수 있게 쓴다. 목록은 사용자 승인 대상(바뀌면 재승인).
    요청사항 실행에서는 요구사항마다 출처를 적는다 — from: "request"(+ quote: 사용자의 말 그대로) · "decision"(+ decision: 계획의 결정
    이름 그대로 — 요구사항은 그 결정에서 지금 고른 안의 것) · "existing"(+ file: 지금 그 동작이 있는 파일) · "needed"(+ for: ["R1"] 이 동작으로 이루는 요구사항).
    사용자가 앱에서 직접 쓴 요구사항은 같은 번호·같은 글로 출처 칸 없이 다시 싣는다 (needed 의 for 로 가리킬 수 있다).
    화면이 있는 작업이면 화면에서 확인할 수 없는 요구사항(저장 방식·API 응답 등)에 screen: false — 나머지는 화면 케이스로 확인한다.
    툴킷은 출처가 사실인지(인용이 사용자의 말에 있나, 파일이 원래 있었나, 결정이 계획에 있나)만 확인한다."""
    from beom_workbench.core import make

    _own_port(project_id, port_id)
    conn, _ = project.load(settings, project_id)
    if requirements is None:
        return {"requirements": make.requirements(conn, port_id)}
    rid = os.environ.get("BEOM_WORKBENCH_RUN")
    run = inbox.load_run(settings, rid) if rid else None
    if run is not None and run.get("port_id") == port_id and run.get("mode") == "request":
        from beom_workbench.modules import run as run_tools
        rows = [dict(r) for r in requirements if isinstance(r, dict)]
        # 번호를 먼저 붙인다 — 출처(for)가 번호로 서로를 가리키고, 툴킷이 번호마다 출처를 남긴다 (원장이 붙이는 방식과 같다)
        old = make.requirements(conn, port_id)
        n = max([int(x[1:]) for x in [str(r.get("id") or "") for r in rows] + [o["id"] for o in old]
                 if make.REQ_ID.fullmatch(x)] + [0])
        for r in rows:
            if not r.get("id"):
                n += 1
                r["id"] = f"R{n}"
        # 사용자가 앱에서 쓴 요구사항을 같은 글로 다시 실으면 출처는 사용자 그대로다 (출처 칸을 비워 둔다). 'user' 는 앱만 붙인다 —
        # 에이전트가 from="user" 를 적으면 모르는 값으로 돌려보낸다 (출처 확인을 건너뛰지 못하게)
        meta_now = run_tools._req_meta(run)
        texts = {o["id"]: o["text"] for o in old}
        users = {k: texts[k] for k, m in meta_now.items() if isinstance(m, dict) and m.get("from") == "user" and k in texts}
        kept = set()
        for r in rows:
            rid_ = str(r["id"])
            if not r.get("from") and rid_ in users and users[rid_] == str(r.get("text") or "").strip():
                kept.add(rid_)
        problems = run_tools.origin_problems(run, [r for r in rows if str(r["id"]) not in kept], user_rows={k: users[k] for k in kept})
        # 걸린 것을 모두 한 번에 — 출처를 고쳐 다시 내면 그제야 값·글 검사에 걸려 한 번을 더 썼다
        parts = (["요구사항의 출처를 고친다: " + "; ".join(problems[:10]) + " — " + run_tools.ORIGIN_HOW] if problems else [])
        gate = run_tools.requirement_gate(run, rows, user_ids=kept)
        if gate:
            parts.append(gate)
        msg = (" / ".join(parts) + ". 모두 고쳐 한 번에 다시 저장한다") if parts else None
        if msg:
            inbox.log(settings, run["id"], "refused", tool="wb_make_requirements", error=msg[:2000])
            raise ToolError("invalid_request", msg)
        out = make.set_requirements(conn, port_id, rows, actor)
        meta = {str(r["id"]): ({"from": "user"} if str(r["id"]) in kept else run_tools.req_origin(r)) for r in rows}
        inbox.update_run(settings, run["id"], lambda x: x.update(req_meta=meta, req_saved_at=inbox._now()))
        return out
    if run is not None and run.get("port_id") == port_id:
        from beom_workbench.modules import run as run_tools
        problem = run_tools.requirement_gate(run, [r for r in requirements if isinstance(r, dict)])
        if problem:
            inbox.log(settings, run["id"], "refused", tool="wb_make_requirements", error=problem[:2000])
            raise ToolError("invalid_request", problem)
    return make.set_requirements(conn, port_id, requirements, actor)


@mcp.tool()
@enveloped("port.status")
def wb_port_status(project_id: str, port_id: str | None = None, detail: bool = False) -> dict:
    """포팅 진행·게이트·next_step(다음 할 일). 게이트: 매핑 · 대상 위치 · 차등 검증(지금 코드 기준) · 케이스 충분성 ·
    사람 승인 · 기록 무결 · 라이선스. 다음에 할 일은 next_step 을 따른다.
    기본은 짧게 (게이트·남은 매핑·승인 대기 항목) — 모든 매핑 줄과 승인 항목의 상세가 필요할 때만 detail=true."""
    conn, _ = project.load(settings, project_id)
    if not port_id:
        return port.list_(conn)
    return port.status_(conn, port_id) if detail else port.agent_status(conn, port_id)


@mcp.tool()
@enveloped("port.capture")
def wb_port_capture(project_id: str, port_id: str, cases_file: str, source_url: str | None = None,
                     source_cmd: str | None = None, source_cli: str | None = None, trusted: bool = False,
                     source_image: str | None = None, explore_n: int = 0, coverage_cmd: str | None = None,
                     coverage_report: str | None = None) -> dict:
    """원본에 케이스(HTTP·CLI·함수 호출)를 돌려 골든 응답을 저장.
    원본 실행: source_image(Docker 격리) 또는 trusted=True(로컬 임시 사본). explore_n: 입력 변형 케이스 추가 생성.
    coverage_report(+coverage_cmd): 원본 커버리지로 케이스가 슬라이스 함수를 모두 거쳤는지 확인(게이트)."""
    _own_port(project_id, port_id)
    inbox.gate_commands(settings, {"source_cmd": source_cmd, "source_cli": source_cli, "coverage_cmd": coverage_cmd,
                                   **port.commands_in_cases(settings, project_id, port_id, cases_file)})
    conn, proj = project.load(settings, project_id)
    out = port.capture(conn, proj, settings, port_id, cases_file, source_url=source_url, source_cmd=source_cmd,
                       source_cli=source_cli, trusted=trusted, source_image=source_image, explore_n=explore_n,
                       coverage_cmd=coverage_cmd, coverage_report=coverage_report)
    # 인계 때에야 알리면 케이스를 다시 짜고 다시 저장·검증하는 데 한 라운드를 더 쓴다 (실험) — 저장하는 이때 미리 알린다
    if os.environ.get("BEOM_WORKBENCH_RUN") and isinstance(out, dict):
        try:
            from beom_workbench.modules import run as run_tools
            run = inbox.load_run(settings, os.environ["BEOM_WORKBENCH_RUN"])
            lack = run_tools._ui_without_screen(run) if run.get("port_id") == port_id else None
        except Exception:  # noqa: BLE001 — 미리 알림을 못 해도 저장은 그대로
            lack = None
        if lack:
            out["warning"] = lack + " (인계 때 툴킷이 확인한다)"
    return out


@mcp.tool()
@enveloped("port.verify")
def wb_port_verify(project_id: str, port_id: str, target_url: str | None = None, target_cmd: str | None = None,
                    target_cli: str | None = None, cases_file: str | None = None, source_url: str | None = None,
                    source_cmd: str | None = None, source_cli: str | None = None, trusted: bool = False,
                    source_image: str | None = None, concurrency: int = 0, target_coverage_cmd: str | None = None,
                    target_coverage_report: str | None = None, kept: list[dict] | None = None) -> dict:
    """대상(내 코드, 어떤 언어든)을 원본과 차등 비교. 기본은 골든 대비, source_* 를 주면 실시간.
    kept: 기존 화면 규칙 결과(screen_rules) 중 고치지 않고 둘 곳 [{at: "app/templates/x.html", why: "사용자에게 보일 까닭"}] —
    그 까닭과 함께 사용자 확인 카드에 올라간다. 고치지도 남기지도 않은 곳은 카드에 오르지 않고 인계가 막힌다.
    concurrency>1: 케이스마다 대상에 동시 요청. 전부 통과하면 원본 처리 함수에 L3 근거(differential).
    프로젝트 테스트·타입 검사도 툴킷이 함께 돌려(project_checks) 완료 게이트에 넣는다 — 따로 돌리지 않아도 된다."""
    for side_cmd, side_url, name in ((target_cmd, target_url, "target"), (source_cmd, source_url, "source")):
        if side_cmd and not side_url:
            # 묻기 전에 돌려보낸다 — 주소 없는 서버 명령은 검증할 수 없어, 승인 카드만 하나 더 뜨고 실패한다
            raise ToolError("invalid_request", f"{name}_cmd 는 검증용 서버를 띄우는 명령 — {name}_url 과 함께 쓴다",
                            recovery=f'CLI 프로그램이면 {name}_cli 에 명령을 넣는다 (예: {name}_cli="python -m mytool"). '
                                     f'서버면 {name}_url="http://127.0.0.1:{{port}}" 를 함께 준다')
    _own_port(project_id, port_id)
    inbox.gate_commands(settings, {"target_cmd": target_cmd, "target_cli": target_cli, "source_cmd": source_cmd,
                                   "source_cli": source_cli, "target_coverage_cmd": target_coverage_cmd,
                                   **port.commands_in_cases(settings, project_id, port_id, cases_file)})
    conn, proj = project.load(settings, project_id)
    run = inbox.load_run(settings, os.environ["BEOM_WORKBENCH_RUN"]) if os.environ.get("BEOM_WORKBENCH_RUN") else None
    if run is not None and run.get("port_id") == port_id and (target_cmd or target_url):
        # 화면을 여는 방법 — 디자인 점검·화면 찍기가 검증과 같은 서버·주소로 열게 남긴다 (경로가 달라 엉뚱한 화면을 잰 일이 되풀이됐다)
        inbox.update_run(settings, run["id"], lambda r: r.update(screen_launch={"cmd": target_cmd, "url": target_url}))
    if run is not None and kept:
        marks = {str(k.get("at") or "").replace("\\", "/").rsplit(":", 1)[0]: str(k.get("why") or "").strip()[:300]
                 for k in kept if isinstance(k, dict) and str(k.get("why") or "").strip() and str(k.get("at") or "").strip()}
        if marks:
            inbox.update_run(settings, run["id"], lambda r: r.update(rules_kept={**(r.get("rules_kept") or {}), **marks}))
    if run is not None:
        from beom_workbench.core import deps
        from beom_workbench.core import tasks
        root_ = Path(run["target_root"])
        tasks.ensure_tools(settings, run, [(k, "verify", tasks.command(root_, k, "test", [], "") or "", root_)
                                           for k in tasks.kinds(root_) if k in tasks.TOOL_OF])
        lack = tasks.ensure_command_tools(settings, run, [target_cmd, target_cli, source_cmd, source_cli])
        if lack:  # 명령을 돌릴 도구가 없다 — 돌려서 이상하게 실패시키지 않는다 (툴킷이 설치를 물었고 사용자가 설치하지 않았다)
            return {"ok": False, "error": f"{', '.join(lack)} 이(가) 이 PC 에 없어 검증 명령을 돌리지 않음 — 사용자가 설치하지 않기로 했다",
                    "recovery": "설치를 다시 묻지 말고 빌드 없이 할 수 있는 만큼 한 뒤 wb_handoff 의 risks 에 하지 못한 검증을 적는다"}
        deps.ensure(settings, run, root_)  # 그다음 의존성 (도구 버전에 맞게) — 바뀐 것이 없으면 바로 지나간다
    res = port.verify(conn, proj, settings, port_id, target_url=target_url, target_cmd=target_cmd, target_cli=target_cli,
                      cases_file=cases_file, source_url=source_url, source_cmd=source_cmd, source_cli=source_cli,
                      trusted=trusted, source_image=source_image, concurrency=concurrency,
                      target_coverage_cmd=target_coverage_cmd, target_coverage_report=target_coverage_report)
    if run is not None and run.get("port_id") == port_id and isinstance(res, dict):
        from beom_workbench.modules import run as run_tools
        left = run_tools.before_handoff(inbox.load_run(settings, run["id"]))
        if left:  # 인계 때 돌려보낼 것 — 인계 검사와 같은 함수로 미리 (놓치거나 건너뛴 것을 인계 때에야 알지 않게)
            res["before_handoff"] = left
    return res


@mcp.tool()
@enveloped("port.finish")
def wb_port_finish(project_id: str, port_id: str, actor: str = "agent") -> dict:
    """완료 판정 — 게이트가 모두 충족될 때만 발급하고, 아니면 미충족 게이트와 next_step 을 돌려준다.
    포팅이 '완료'라고 보고할 때는 이 결과(finished id)로만 말한다."""
    _own_port(project_id, port_id)
    conn, _ = project.load(settings, project_id)
    return port.finish(conn, port_id, actor)


@mcp.tool()
async def wb_port_approve(project_id: str, port_id: str, ctx: Context) -> dict:
    """사람 확인 항목(차이 허용·생략·케이스 변경)을 사용자에게 확인 창으로 보여 주고 승인받는다.
    응답은 사용자가 직접 한다 — 에이전트가 대신 답할 수 없다. 클라이언트가 확인 창을 지원하지 않으면
    사용자에게 터미널에서 `beom-workbench port-approve <project> <port>` 를 직접 실행해 달라고 요청한다."""
    env = Envelope("port.approve")
    if os.environ.get("BEOM_WORKBENCH_RUN"):
        return env.fail("denied", "자동 실행 중에는 에이전트가 승인을 요청하지 않는다 — 사용자가 앱에서 승인한다")
    try:
        conn, _ = project.load(settings, project_id)
        items = port.pending_reviews(conn, port_id)
        if not items:
            return env.ok({"approved": [], "note": "승인 대기 없음"})
        message = f"포팅 {port_id} — 승인 대기 {len(items)}건\n\n{port_guard.render(items)}\n\n모두 승인하시겠습니까?"
        try:
            res = await ctx.elicit(message=message, schema=_Approval)
        except Exception as e:  # noqa: BLE001 — 확인 창 미지원 클라이언트
            raise ToolError("human_required", f"확인 창을 띄우지 못함 ({type(e).__name__})",
                            recovery=f"사용자에게 터미널에서 'beom-workbench port-approve {project_id} {port_id}' 를 직접 실행해 달라고 요청") from None
        if res.action != "accept" or not getattr(res.data, "approve", False):
            return env.ok({"approved": [], "note": f"사용자가 승인하지 않음 ({res.action})"})
        # 확인 창을 띄운 사이에 내용이 바뀌었으면 승인하지 않는다
        now = {(i["item_id"], i["fingerprint"]) for i in port.pending_reviews(conn, port_id)}
        chosen = [i for i in items if (i["item_id"], i["fingerprint"]) in now]
        done = port_guard.approve(conn, port_id, chosen, channel="mcp-elicitation", approved_by="user(elicitation)")
        st = port.status_(conn, port_id, brief=True)
        return env.ok({"approved": done, **{k: st[k] for k in ("gates", "next_step", "approvals")}})
    except ToolError as e:
        return env.fail(e.code, e.message, e.recovery, e.data)
    except Exception as e:  # noqa: BLE001
        return env.fail("internal", f"{type(e).__name__}: {e}")
