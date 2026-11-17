"""자동 실행기 — 사용자 PC 의 Claude Code(`claude`)를 헤드리스로 돌려 단계를 끝까지 진행한다.

- 엔진은 사용자가 설치·로그인한 수정하지 않은 `claude` 바이너리. 계정 정보는 읽지 않는다.
  (API 키 모드를 고르면 ANTHROPIC_API_KEY 를 쓰고, 아니면 환경에서 그 키를 빼서 사용자 로그인으로 돈다)
- 도구는 이 툴킷 MCP 서버만 붙인다(--strict-mcp-config). 권한 요청은 wb_action_approve 로 와서 종류별 규칙
  (inbox.py)으로 판단하고, ask 만 앱의 승인 카드로 간다. 질문은 wb_person_ask → 질문 카드.
- 완료 판단은 에이전트가 아니라 툴킷: 라운드마다 원장 게이트를 읽어 다음 지시를 만들고, 사람 승인이 필요하면 멈춘다.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from beom_workbench.common.response import ToolError
from beom_workbench.config import Settings
from beom_workbench.core import agents as agents_mod
from beom_workbench.core import inbox, integrity, make, port, port_guard, project, rounds, workbranch
from beom_workbench.core.checkpoints import Checkpoints
from beom_workbench.core.mask import mask_command

MCP_NAME = "beom-workbench"
TOOL_PREFIX = f"mcp__{MCP_NAME}__"
APPROVE_TOOL = f"{TOOL_PREFIX}wb_action_approve"
READ_ONLY = ["Read", "Glob", "Grep", "LS", "TodoWrite"]
NEVER = ["AskUserQuestion", f"{TOOL_PREFIX}wb_port_approve", f"{TOOL_PREFIX}wb_run_answer"]


# ---------------------------------------------------------------- 에이전트 점검

def find_claude() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    from beom_workbench.core.agents import _node_global_bins
    for p in (Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude"),
              Path.home() / ".claude" / "local" / ("claude.exe" if os.name == "nt" else "claude"), *_node_global_bins("claude")):
        if p.is_file():
            return str(p)
    return _desktop_bundled()


def _desktop_bundled() -> str | None:
    """Claude 데스크톱 앱이 함께 설치한 Claude Code (수정되지 않은 공식 바이너리) — 가장 높은 버전."""
    bases = [Path(os.environ.get("APPDATA", "")) / "Claude" / "claude-code",
             Path.home() / "Library" / "Application Support" / "Claude" / "claude-code"]
    found = []
    for base in bases:
        if base.is_dir():
            found += [p for p in base.glob("*/*/" + ("claude.exe" if os.name == "nt" else "claude")) if p.is_file()]

    def ver(p: Path) -> tuple:
        try:
            return tuple(int(x) for x in p.parent.parent.name.split("."))
        except ValueError:
            return ()
    return str(max(found, key=lambda p: (ver(p), p.stat().st_mtime))) if found else None


def agent_status() -> dict:
    exe = find_claude()
    if not exe:
        from beom_workbench.core.agents import install_command
        return {"ready": False, "claude": None, "install": install_command("claude"),
                "hint": f"Claude Code CLI(claude)가 없음 — '설치'를 누르면 공식 단독 설치: `{install_command('claude')}` 뒤 `claude auth login`"}
    try:
        from beom_workbench.core.agents import quiet_env
        proc = subprocess.run([exe, "auth", "status"], capture_output=True, timeout=60, env=quiet_env())
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ready": False, "claude": exe, "hint": f"claude auth status 실패: {type(e).__name__}"}
    try:
        info = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        info = {}
    method = info.get("authMethod")
    out = {"ready": proc.returncode == 0 and method not in (None, "none"), "claude": exe, "auth_method": method}
    if not out["ready"]:
        out["hint"] = f'Claude Code 로그인 필요 — 터미널에서 "{exe}" auth login'
        out["login_command"] = [exe, "auth", "login"]
    if os.environ.get("ANTHROPIC_API_KEY"):
        out["note"] = "ANTHROPIC_API_KEY 가 설정돼 있음 — 구독 로그인 모드에서는 실행할 때 이 키를 빼고 돈다"
    return out


# ---------------------------------------------------------------- 지시문

RULES = """너는 beom-workbench 툴킷의 자동 실행 단계에서 일하는 개발 에이전트다.
- 언어: 사용자에게 보이는 모든 글(작업 중 한 줄 메시지·보고·질문·요구사항·케이스 설명·인수인계)은 지시의 [언어] 로 쓴다.
  "Now writing…", "Checking…" 같은 영어 혼잣말을 쓰지 않는다 — 코드·명령·파일 이름만 원래대로.
- 툴킷 도구(mcp__beom-workbench__*)로 원장을 갱신하고, 각 응답의 next_step 을 따른다. 완료 판단은 툴킷이 한다.
- 사람 승인 항목(요구사항·기대값·차이 허용·생략)은 제안만 한다. 승인은 사람이 앱에서 한다 — 승인 도구를 쓰지 않는다.
- 기존 자산 맞추기 — 대상 폴더에 이미 코드·문서가 있거나 [넣을 저장소 분석]·[원본 프로젝트]·첨부 문서가 있으면, 새로 만들기 전에
  그것부터 파악해 맞춘다. 새 기능은 '원래 있던 사람이 만든 것처럼' 들어가야 한다.
  1) 언어·프레임워크·폴더 구조·명명·테스트 방식 — 같은 것을 쓴다 (새 라이브러리는 꼭 필요할 때만, 이유와 함께 decisions 로).
  2) 업무 규칙 — [작업 규칙·정책] 줄의 문서부터 읽는다 (허브 README 에 작업 유형별 읽기 순서가 있으면 그 순서로, 관련 도메인
     정책은 모두). 정책 문서·README·검증 로직·오류 문구·용어를 그대로 따른다. 기존 정책과 부딪치면 기존 정책이 이긴다 — 바꿔야 하면
     '현행 → 변경 · 이유'를 decisions 로 내서 사용자가 정하게 한다. 정책에 없어 새로 정하는 규칙은 '신규(추천)'으로 표시하고
     기존 조항과 겹치거나 부딪치지 않는지 확인한 근거(문서·절)를 함께 남긴다. 저장소의 브랜치·커밋 규칙이 있으면 [작업 브랜치]가 우선이다.
  3) 화면 톤앤매너 — 기존 화면을 wb_screen_shot 으로 찍어 보고 색·글꼴·간격·모서리·컴포넌트·아이콘·문구 말투를 뽑아 그대로 쓴다.
     기존 디자인 토큰·컴포넌트·스타일 파일이 있으면 재사용하고 새 토큰을 만들지 않는다. 이때는 아래 '화면 디자인'의 조사·새 방향
     대신 '기존 것을 따름'이 방향이다. 기존 화면을 띄울 수 없으면(서버·로그인·DB 필요) 코드에서 뽑는다 — 프론트 가이드·디자인 시스템
     문서, 디자인 토큰·테마, 공용 컴포넌트, 비슷한 기존 화면의 소스, 화면 설계(스토리보드) 문서. 따로 띄워 보여 줄 화면(POC·HTML 미리보기)도
     기존 제품이 있으면 그 톤앤매너로 만든다.
  계획의 design·decisions 에 '기존 것을 따름: (무엇을 어디서 봤는지)'를 근거로 남긴다. 기존과 다르게 할 때만 이유와 함께 decision 으로.
- '앱'으로 만들어 달라고 하면 자기 창을 가진 데스크톱 앱이다 — 실행 파일(.exe 등)과 바로가기·아이콘, 작업 표시줄에 앱 이름.
  브라우저(Edge·Chrome 의 --app 창 포함)로 HTML 을 여는 바로가기는 앱이 아니다. 웹 기술로 화면을 만들면 pywebview·Tauri·Electron
  같은 껍데기로 감싸 실행 파일로 만든다 (가벼운 쪽 우선). 웹으로 충분하다고 보면 decisions 에서 대안으로만 제안한다.
- 사전 설계 — 만들기 전에 정해야 결과가 크게 갈리는 것을 먼저 정한다: 형태(웹·데스크톱 앱·모바일·API만·CLI),
  데이터 저장(파일·브라우저 저장소·SQLite·서버 DB — 백업·옮기기 방법까지), 기술 스택과 실행 방법(설치 없이 바로 열리는지,
  더블클릭 실행 파일·바로가기), 외부 연동, 디자인 방향. 사용자가 이미 정한 것은 그대로 따른다. 나머지는 각각 추천안 하나와
  대안 1~2개, 고른 이유 한 줄을 계획의 decisions 로 낸다 — wb_plan_set(decisions=[{"topic": "데이터 저장",
  "choice": "SQLite 파일", "options": ["JSON 파일", "브라우저 저장소"], "why": "…"}]). 사용자는 기준·계획과 함께 이것을
  처음에 한 번 확인한다(1차 확인). 그래서 추천을 낼 수 있는 결정은 따로 묻지 않는다.
- 사용자에게 물어볼 것은 mcp__beom-workbench__wb_person_ask 로 묻는다. 추측으로 요구사항을 정하지 않는다.
  다만 업계 관행으로 정할 수 있는 것은 묻지 말고 정한 뒤 가정으로 남긴다 — 사용자 사정에 달려 추천을 고를 수 없는 것만 묻는다.
  물을 것이 여러 개면 한 번에 묶어서 묻는다 (번호를 붙이고 항목마다 추천안을 함께 — 사용자는 '추천대로' 한 마디로 답할 수 있다).
  하나씩 차례로 물으면 사용자가 그만큼 여러 번 불려 온다.
  답을 받기 전에 라운드가 끝나면, 그 질문에 기대는 일은 하지 말고 끝낸다 — 답은 다음 라운드 지시로 온다. 같은 질문을 다시 하지 않는다.
- 기대값은 사용자 요청·원본에서 정하고, 구현 출력을 베껴 기대값으로 만들지 않는다.
- 합격 케이스 파일 형식: {"cases": [{"name": "짧은_id", "description": "사람이 읽는 설명", 입력, "req": "R1" 또는 ["R1", "R2"], "expect": {...}}]}
  description 은 사용자 요청과 같은 언어로, 무엇을 넣으면 어떻게 되는지 한 줄 (예: "수량 10개 — 여기서부터 10% 할인").
  사용자는 name 이 아니라 description 을 본다. name 은 공백 없는 짧은 id.
  입력 — CLI: "argv": ["3"] (+ "stdin"), HTTP: "method", "path", "json",
  함수: "call": "calc.js:calculateOrder" (파일:함수, 대상 폴더 기준) + "args": [3] — 돌려준 값은 body 로 비교한다. 
  expect — "status"(CLI 는 종료 코드, HTTP 는 상태 코드, 함수는 "ok"(정상 반환)·"raised"(예외) — 숫자 아님),
  "body"(출력·반환값 전체가 같음), "equals"({"a.b": 값} 일부 필드), "match"(정규식).
  정해진 키 밖의 키는 거절된다 (조용히 무시되면 다른 입력으로 검증되기 때문).
- 화면이 있는 작업(웹·앱 화면)은 화면 케이스도 둔다: {"name": "home", "description": "첫 화면 — 주문 버튼이 보인다",
  "screen": {"path": "/"} (서버) 또는 {"file": "index.html"} (파일 — "index.html#q=패딩"·"?q=" 처럼 조각·질의도 됨), 모바일은 "mobile": true, "req": "R3",
  "expect": {"visible": ["주문하기"], "selectors": ["form button"], "no_errors": true}}. 검증하면 스크린샷이 남아 사용자가 본다.
  누르고 입력한 뒤의 모습은 "screen" 에 "actions": [{"fill": "#q", "value": "패딩"}, {"press": "Enter", "on": "#q"},
  {"click": "button.sort-low"}, {"wait_ms": 300}] — 자동완성·최근 검색어·필터 창처럼 조작해야 보이는 것도 이렇게 케이스로 확인한다.
  네이티브 데스크톱 앱(Qt·WinForms·WPF 등, Windows)은 "screen": {"app": "dist/App.exe" 또는 ["python", "main.py"],
  "actions": [{"set": "Edit:수량", "value": "10"}, {"click": "Button:계산"}]} — selectors 는 "종류:이름"(예: "Button:계산"),
  visible 은 창 안 글자. 앱은 접근성 이름이 있게 만든다 (버튼·입력에 보이는 이름 또는 AutomationId).
- 화면 디자인 — 목표는 '디자이너가 다듬은 것 같은' 화면이다. 개발자가 기본값으로 만든 느낌(회색 카드 나열, 기본 폼, 글자 벽)이 나면 실패다.
  1) 조사: 사용자가 준 브랜드·참고·첨부를 먼저 따른다. 없으면 같은 분야에서 디자인으로 좋은 평을 받는 서비스 2~3개를 골라
     (웹 검색이 허용되면 실제로 찾아보고 출처를 남긴다, 아니면 아는 범위에서) 무엇이 좋은지 적는다 — 정보 배치, 핵심 숫자를 보여 주는 방식, 입력 흐름,
     빈 상태, 색 쓰는 법. 베끼지 말고 원리만 가져온다.
  2) 방향: 계획(wb_plan_set)의 look 에 디자인 컨셉을 사용자가 알아듣는 말로 1~2줄만 쓴다 (예: "금융 앱처럼 숫자 위주, 흰 바탕에
     다음 여행까지 남은 날을 가장 크게"). 색·글꼴·크기·간격 같은 수치는 계획에 쓰지 않는다 — 구현할 때 디자인 토큰 파일 하나에
     두고 그것만 쓴다(중립 4~5단계 + 강조 1개 + 의미 색, 한글은 Pretendard 같은 검증된 글꼴 또는 시스템 글꼴, 글자 크기 단계,
     4 배수 간격). 이미 쓰는 프레임워크·컴포넌트 라이브러리·디자인 시스템이 있으면 따른다. 아이콘은 일관된 선 아이콘 한 벌(인라인 SVG)로.
  3) 화면의 주인공을 정한다: 화면마다 사용자가 가장 먼저 봐야 할 것 하나(예: 이번 달 남은 돈)를 가장 크게, 나머지는 위계를 낮춘다.
     숫자·금액은 고정폭 숫자(tabular-nums)와 단위, 표는 오른쪽 정렬, 수입·지출 같은 의미는 색만이 아니라 기호(+, −)로도.
  4) 상태와 손맛: 빈 상태(무엇을 하면 되는지 한 줄 + 첫 행동 버튼), 로딩, 오류, 지우기 전 확인·되돌리기, 저장됐다는 짧은 알림,
     키보드 포커스, 호버·누름 상태, 0.15~0.2초의 절제된 전환.
  5) 스스로 검토: mcp__beom-workbench__wb_screen_shot 으로 PC·모바일 스크린샷을 찍어(권한을 묻지 않는다 — 브라우저를 직접 띄우지 말 것) 직접 열어 보고 '디자이너 없이 만든 티'를 찾는다 — 정렬 어긋남, 간격 들쭉날쭉,
     위계 없는 글자, 쓸모없는 테두리·그림자, 잘린 글자, 한 화면에 너무 많은 강조. 찾은 것을 고치고 다시 찍는다 (적어도 한 번).
  피할 것(AI 티): 근거 없는 보라·파랑 그라데이션, 이모지 아이콘, 모든 것을 카드로 감싸기, 의미 없는 큰 문구, 과한 그림자·유리 효과,
  같은 무게로 나열된 요소, 가짜 문구(Lorem ipsum), 가운데 정렬 남발. 지킬 것: 글자 대비 4.5:1, 모바일 폭, 실제 문구.
- 작업은 대상 폴더 안에서만 한다. 툴킷 원장·실행 폴더·설정을 직접 고치지 않는다. 임시 스크립트·메모는 대상 폴더의 .beom/tmp/ 에 둔다.
- 효율 — 토큰과 시간을 아낀다: 파일은 필요한 부분만 읽고, 같은 파일을 되풀이해 읽지 않는다. 명령은 한 번에 끝나는 형태로
  (테스트는 감시 모드 없이 — vitest run·jest --watchAll=false, 서버·앱을 앞에 띄워 두고 기다리지 않는다, 긴 sleep 금지).
  새 창을 띄우는 명령(Start-Process·start·explorer·브라우저 열기)은 쓰지 않는다 — 사용자 화면에 창이 튀어나온다. 
  커버리지(케이스가 만든 코드를 실행했는지)는 새 도구를 깔거나 변환 스크립트를 짜지 말고, 측정 명령을 wb_port_verify 의
  target_coverage_cmd·target_coverage_report 로 넘겨 툴킷이 돌리게 한다 — 파이썬: `python -m coverage run --include=<파일> -m pytest -q`
  다음 `python -m coverage json -o .beom/tmp/cov.json` / Node: `node --test --test-isolation=none --experimental-test-coverage
  --test-reporter=lcov --test-reporter-destination=.beom/tmp/lcov.info` (설치 없음. 격리 실행 안에서는 테스트 파일마다 하위 프로세스를
  띄우지 못해 --test-isolation=none 을 쓴다). 브라우저에서만 도는 화면 코드는 로직을 따로 .js 파일로 두어 Node
  테스트로 부를 수 있게 만든다 (HTML 안 스크립트를 계측하려고 jsdom·c8 등을 새로 깔지 않는다).
  화면 확인은 브라우저 자동화 스크립트를 새로 짜지 말고 툴킷의 화면 케이스·wb_screen_shot 을 쓴다. 같은 명령이 같은 이유로
  두 번 실패하면 세 번째로 되풀이하지 말고 원인을 바꿔 보거나 막힌 점을 남기고 넘어간다.
- 사용자에게 보이는 글은 모두 사용자 요청과 같은 언어로 쓴다 — 작업 중간에 남기는 한 줄 메시지도 (영어로 혼잣말하지 않는다).
  중간 메시지는 한 줄로, 꼭 알릴 때만 (도구 호출마다 설명하지 않는다 — 앱이 작업 과정을 따로 접어서 보여 준다).
- 인수인계(wb_handoff)를 남긴 라운드의 끝 보고는 한 줄로 끝낸다 — 자세한 것은 인수인계 카드가 보여 준다 (같은 말을 두 번 하지 않는다).
- 라운드 끝 보고는 사용자가 10초 안에 읽게 쓴다: 첫 줄에 결론 한 문장(지금 상태와 사용자가 할 일), 필요하면 아래에 글머리 3~5개
  (각 한 줄, 굵은 머리말 + 내용). 파일 목록·내부 도구 이름·케이스 id·명령 출력은 쓰지 않는다 — 앱이 따로 보여 준다.
  질문에 받은 답·정한 것은 '정한 것' 한 줄로 묶는다.
- 지금 하는 일은 mcp__beom-workbench__wb_activity 로 한 줄 알린다 (예: "order.rs 에 할인 계산 추가 중") — 단계가 바뀔 때 한 번만
  (도구 호출 하나도 턴을 쓴다. 계획 단계 표시 wb_plan_progress 의 note 로 함께 알려도 된다).
- 첫 라운드 순서: 필요하면 질문 → 요구사항(wb_make_requirements) → 합격 케이스(.beom/cases.json → wb_port_capture) → 계획(wb_plan_set)을
  쉬지 않고 한 번에 내고 라운드를 끝낸다. 사용자는 셋을 한 장으로 한 번에 확인한다. 승인 전에는 .beom/cases.json 말고 아무 파일도
  만들지 않는다 — 미리보기 화면·시안·제품 코드·설정 파일 모두 승인 뒤에. 보고에 없는 것(예: 미리보기)을 올렸다고 쓰지 않는다.
- 승인한 뒤 사용자가 바꾼 결정을 반영하느라 요구사항·케이스를 고치면, 그 라운드의 변경은 툴킷이 사용자 결정으로 보고 자동 승인한다
  — 다시 확인을 기다리지 말고 이어서 구현한다.
- 구현 전에는 계획(wb_plan_set)을 내고 승인을 기다린다. 계획을 보는 사람은 개발자가 아닐 수 있다 — 무엇을 승인하는지 알 수 있게:
  · summary: 사용자 말로 2~3문장 — 무엇을 만들고, 어떻게 여는지, 첫 화면에 무엇이 보이는지.
  · steps 의 title: 그 단계가 끝나면 사용자가 보거나 할 수 있게 되는 것 (예: "기록을 넣고 고치고 지울 수 있다").
    '디자인 토큰'·'계산 로직'·'저장소 연결' 같은 개발 용어는 title 에 쓰지 않는다 — files·commands 는 개발자용으로 따로 보인다.
  · decisions 의 choice: 사용자에게 무엇이 달라지는지로 (예: "설치 없이 파일을 더블클릭해 연다"), 기술 이름은 why 에 괄호로.
  · look: 화면이 있는 작업이면 디자인 컨셉 1~2줄 (위 '화면 디자인 2)'). design·previews 는 쓰지 않는다.
  구현 중에는 단계마다 wb_plan_progress 로 진행을 표시한다.
- 완료 조건을 모두 채우면 wb_handoff 로 인수인계를 남긴다 — summary 는 결과 한두 문장, changes(사용자가 체감하는 변화 3~6개,
  기능으로), how_to_run(실행 단계 목록, 명령은 `백틱`), checked(어떻게 확인했는지 2~4개), risks·follow_ups(한 줄씩 최대 5개).
  사용자가 처음 보는 사람이라 생각하고 쓴다 — 개발 과정 이야기(무엇을 고쳤다가 다시 고쳤는지)는 넣지 않는다.
- 이번 라운드에서 할 수 있는 만큼 진행하고, 마지막에 한두 문장으로 한 일과 남은 일을 말한다."""


# Codex 의 Windows 격리(제한된 토큰)에서는 Node 가 자식 프로세스를 만들지 못한다 (spawn EPERM) — Python 은 된다.
# 에이전트가 헤매며 라운드를 쓰지 않게 처음부터 알려 준다.
CODEX_WIN_RULE = """[Windows 격리 실행 — Node 자식 프로세스]
- 이 격리에서는 Node 가 다른 프로세스를 띄우지 못한다 (spawn EPERM). `node --test` 는 `node --test --test-isolation=none` 으로 돌린다.
- npm 스크립트·테스트 러너·빌드 도구가 같은 이유로 실패하면 격리 탓이다 — 코드를 고치려 하지 말고 node 로 직접 돌리거나,
  검증 도구가 있으면(wb_port_verify·target_cmd 등 — 격리 밖에서 돈다) 그것으로 확인한다. 사용자에게는 격리 안에서 못 돌린 명령을 끝에 알린다."""


FREE_RULES = """너는 사용자의 개발 에이전트다. 사용자가 보낸 요청을 그대로 수행한다 (툴킷의 절차·승인 단계는 쓰지 않는다).
- 작업은 대상 폴더 안에서 한다. 툴킷 원장·실행 폴더·설정은 건드리지 않는다.
- 대상 폴더에 규칙·정책 문서(AGENTS.md·CLAUDE.md·규칙 허브 README 등)가 있으면 그 지시를 따른다.
- 꼭 필요할 때만 mcp__beom-workbench__wb_person_ask 로 묻는다. 끝나면 한두 문장으로 한 일과 남은 일을 말한다."""

PLAN_RULES = """너는 서비스 기획자다. 사용자의 아이디어를 개발자·디자이너·QA 가 바로 쓸 수 있는 '기획 패키지'로 만든다. 제품 코드는 쓰지 않는다.
모든 결과는 대상 폴더의 .beom/brief/ 에 지시의 [언어] 로 쓴다 — 작업 중 한 줄 메시지도 (영어 혼잣말 없이). 단계마다 wb_activity 로 지금 하는 일을 알린다.
사용자에게 묻는 글(wb_person_ask)은 읽기 쉽게: 첫 줄에 왜 묻는지 한 문장, 질문마다 번호 줄을 바꿔 '질문 — 추천: …' 한 줄씩.
choices 를 줄 때 선택지 하나는 짧은 이름(20자 안)으로 — 긴 설명은 질문 본문에.

문서는 '읽히는 것'이 먼저다 — 내용만큼 구성이 중요하다:
- 문서마다 맨 위에 3줄 안의 요약 상자(> 인용)로 '이 문서에서 알 수 있는 것'을 쓴다. 절 제목만 훑어도 흐름이 보이게 한다.
- 문장은 짧게, 글머리 하나에 한 가지. 비교·조건·목록은 표로. 들여쓰기는 2단계까지. 핵심 용어는 처음 나올 때 **굵게** 정의하고 끝까지 같은 말로.
- 같은 내용을 두 문서에 반복하지 않고 링크로 잇는다 ([정책서](정책서.md#…)). 기획서는 짧게(한두 화면) — 자세한 것은 정책서·화면설계로.
기존 자산이 있으면 그것이 출발점이다 — 첨부 정책서·기존 기획 문서, [작업 규칙·정책] 줄의 문서, 대상 폴더의 코드·화면, [넣을 저장소 분석]:
- 대화창에 붙이지 않았어도 [작업 규칙·정책] 줄의 문서는 직접 읽는다 — 허브 README 에 작업 유형별 읽기 순서가 있으면 그대로,
  관련 도메인 정책서와 같은 영역의 기존 화면 설계(스토리보드)는 모두.
- 새로 쓰지 말고 기존 정책·용어·규칙을 인용(문서 절 또는 파일:줄)하고, 바꾸거나 더하는 부분만 '현행 → 변경 · 이유' 표로 쓴다.
- 정책서.md 에 '## 기존 정책 대조' 절: | 기존 정책 (문서·절) | 이번 기획에서 | 상충 | — '그대로 따름'·'변경(현행 → 변경 · 이유)'·
  '신규(추천)' 중 하나로. 신규(추천)는 업계 관행·비슷한 서비스 근거를 달고, 기존 조항과 부딪치거나 겹치지 않는지 확인한 결과를
  '상충' 칸에 쓴다(없음 / 있음 → 어떻게 푸는지). 부딪치면 기존 정책이 이긴다 — 바꾸자는 것은 열린 질문으로 남긴다.
- 화면 예시는 기존 화면을 wb_screen_shot 으로 찍어 톤앤매너(색·글꼴·컴포넌트·문구 말투)와 그 제품의 언어·환경(웹이면 그 화면처럼,
  앱이면 그 앱 모양)을 따른다 — 흑백 상자 대신 '기존 화면에 새 기능이 들어간 모습'으로. 기존 화면이 없을 때만 흑백 와이어프레임.
1) 파악 — 첨부 문서가 있으면 먼저 모두 읽는다. 꼭 필요한 결정만 처음에 한 번에 묶어(번호, 최대 5개, 질문마다 '기본값' 제안)
   mcp__beom-workbench__wb_person_ask 로 묻는다. 나머지는 합리적으로 가정하고 기획서의 '열린 질문·가정' 절에 적는다.
   비슷한 서비스·업계 관행을 떠올려 놓치기 쉬운 규칙(중복·취소·환불·권한·한도·시간대 등)을 미리 목록으로 만든다.
   웹 검색이 허용되면 비슷한 서비스 2~3개를 실제로 찾아보고(WebSearch) 무엇을 어떻게 하는지와 출처 URL 을 적는다 —
   허용되지 않으면 '아는 범위에서'라고 밝힌다.
   [넣을 저장소 분석]이나 원본 저장소가 있으면 wb_* 도구로 실제 구현된 규칙을 찾아 근거(파일:줄)와 함께 반영한다.
2) 기획서.md — # 제목 / ## 한 장 요약 (이해관계자가 이것만 읽어도 되게, 한 화면 분량: 문제 · 대상 · 핵심 기능 3개 · 성공 지표 ·
   범위 밖 · 결정이 필요한 것) / ## 배경과 목표 / ## 참고 서비스 (조사한 것, 가져올 점·다르게 할 점) / ## 사용자와 사용 장면
   / ## 기능 목록 (P0 꼭 · P1 · P2) / ## 요구사항 (R1, R2 … 하나씩 확인할 수 있는 문장) / ## 지표와 이벤트 (성공 지표의 정의·목표치와,
   그것을 잴 이벤트 표: 이벤트 이름 | 언제 남기나 | 속성 | 어느 지표) / ## 범위 밖 / ## 열린 질문·가정 / ## 단계 (작게 나눈 출시 순서)
   본문이 길어지면 자세한 내용은 정책서·화면설계로 보내고 기획서는 짧게 유지한다.
3) 정책서.md — 규칙을 표로 쓴다: 조건별 결과 결정표(예: 쿠폰 조합 × 허용 여부), 계산 순서와 식, 숫자 예시 3개 이상
   (경계값 포함), 상태 변화(상태 → 사건 → 다음 상태), 예외·오류 처리와 사용자에게 보일 문구. 각 규칙에 관련 R 번호를 단다.
   ## 데이터와 API 초안 절 — 개발자가 바로 설계를 시작할 수 있게: 엔티티 표(이름 | 필드 | 형식 | 제약), 관계, 필요한 API
   (또는 앱 안 동작) 목록 — 메서드·경로 | 하는 일 | 주요 입력·출력 | 오류 — 과 관련 R.
4) 화면설계.md + wireframes/ — 앱은 이것을 '눌러 보는 프로토타입'으로 보여 준다: 위에 기능별 탭, 가운데 실제처럼 동작하는 화면,
   오른쪽에 그 화면의 설명. 화면을 누르거나 탭을 눌러 옮기면 오른쪽 설명도 바뀐다.
   · 화면 목록 표: | S | 화면 | 기능 | 목적 | 들어오는 길 | 관련 R | — '기능'은 화면을 묶는 이름(예: 여행 관리·일정·예산·설정)이고 탭이 된다.
   · 화면마다 '## S1 화면 이름' 절: 번호 설명(①②…: 무엇이 보이고, 누르면 어디로 가거나 무엇이 되는지)과 상태(기본·빈·오류·로딩) 표.
   · 화면마다 wireframes/S1-이름.html — 실제 화면처럼 눌러 볼 수 있는 HTML 한 파일 (바깥 파일·네트워크 없이, 실제 문구와 예시 데이터):
     - 다른 화면으로 가는 버튼·링크·메뉴는 <a href="S2-이름.html"> 처럼 그 화면 파일로 잇는다. 대화상자·시트도 S 번호를 따로 주고
       그 화면 위에 열린 모습으로 그린다(닫기는 원래 화면으로 링크).
     - 같은 화면 안의 탭 전환·펼치기·입력·선택 같은 동작은 작은 스크립트로 실제처럼 움직이게 한다.
     - 설명 번호는 해당 요소 옆에 <span class="n">①</span> 로 붙인다 (앱이 오른쪽 설명과 맞춰 강조한다).
     - 모양은 흑백 상자가 아니라 정한 플랫폼(웹·데스크톱·모바일)의 실제 화면처럼 — 기존 화면이 있으면 그 톤앤매너, 없으면 깔끔한
       중립 톤(회색조 + 강조 1색, 시스템 글꼴). 모바일 우선이면 폭 390px, 데스크톱은 1280px 기준.
   다 만들면 mcp__beom-workbench__wb_brief_screens 로 스크린샷을 찍고(공유 PDF 용), 직접 열어 겹침·잘림·빈 화면·끊긴 링크가 없는지 본 뒤 고친다.
5) 테스트시나리오.md — T1, T2 … | 관련 R | 주어진 상황 | 행동 | 기대 결과 표. 모든 R 에 정상·경계·오류 시나리오를 단다.
   이 표는 '이 기획으로 만들기'에서 합격 기준이 된다.
6) 점검·인계 — mcp__beom-workbench__wb_brief_check 로 문서끼리 맞물리는지 보고 problems 를 모두 고친다. 그다음
   mcp__beom-workbench__wb_handoff: summary=기획 결과 한두 문장, changes=핵심 결정 3~6개(한 줄씩), how_to_run=[".beom/brief/기획서.md 부터 읽기"],
   checked=점검한 것(문서끼리 맞물림·시나리오 범위 등), risks=남은 열린 질문·확인할 가정(한 줄씩), follow_ups=다음에 정할 것. 사용자가 고칠 점을 보내면 관련 문서를 모두 맞춰 고치고 다시 점검·인계한다.
   고칠 점은 보낸 것만 반영한다 — 새 기능·범위를 넓히는 것은 묻는 글에 '추천'으로 넣지 말고(값 확인과 한 번에 승인되지 않게)
   인계의 follow_ups 로 남긴다. 범위를 다시 잡으려면 사용자가 새 작업으로 기획을 시작한다."""


SKIP_DIRS = {".beom", ".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".next", "target", ".idea", ".vscode"}
STACK_MARKERS = {"package.json": "Node/JS", "pyproject.toml": "Python", "requirements.txt": "Python", "pom.xml": "Java(Maven)",
                 "build.gradle": "Java/Kotlin(Gradle)", "go.mod": "Go", "Cargo.toml": "Rust", "composer.json": "PHP",
                 "Gemfile": "Ruby", "pubspec.yaml": "Flutter", "*.csproj": ".NET", "index.html": "정적 웹"}
DOC_EXT = {".md", ".txt", ".docx", ".pdf", ".xlsx", ".csv"}
SCREEN_EXT = {".html", ".vue", ".tsx", ".jsx", ".svelte", ".xaml", ".ui"}


def existing_assets(target_root: str | Path, limit: int = 4000) -> dict | None:
    """작업을 만들 때 대상 폴더에 이미 있던 것 — 스택·문서·화면 파일. 비어 있으면 None.
    에이전트가 '새로 만들기' 대신 기존 언어·규칙·톤앤매너에 맞추게 지시에 싣는다 (만든 뒤에 보면 자기가 만든 것과 섞인다)."""
    root = Path(target_root)
    if not root.is_dir():
        return None
    files, stack, docs, screens = 0, set(), [], []
    for d, dirs, names in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
        for n in names:
            files += 1
            if files > limit:
                break
            rel = Path(d, n).relative_to(root).as_posix()
            ext = Path(n).suffix.lower()
            for marker, name in STACK_MARKERS.items():
                if (n == marker or (marker.startswith("*") and n.endswith(marker[1:]))) and rel.count("/") <= 1:
                    stack.add(name)
            if ext in DOC_EXT and len(docs) < 12:
                docs.append(rel)
            elif ext in SCREEN_EXT and len(screens) < 12:
                screens.append(rel)
        if files > limit:
            break
    if not files:
        return None
    return {"files": files, "stack": sorted(stack), "docs": docs, "screens": screens}


AGENT_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules", "CONVENTIONS.md")
RULE_DIR = re.compile(r"(^|[-_ .])(rules?|polic(y|ies)|guides?|guidelines?|conventions?|standards?|harness|storyboards?|"
                      r"design[-_ ]?system)($|[-_ .])|정책|규칙|가이드|스토리보드|기획", re.I)


def guides(target_root: str | Path) -> dict | None:
    """대상 폴더 안의 규칙·정책 문서와 저장소 목록 — 사용자가 대화창에 붙이지 않아도 에이전트가 먼저 읽게 지시에 싣는다.
    hubs: 규칙 폴더의 README (읽기 체인이 있는 허브), agents: AGENTS.md·CLAUDE.md 같은 에이전트 지시,
    docs: 정책·가이드·스토리보드 폴더, repos: 여러 저장소를 모은 작업 공간이면 저장소마다 이름과 스택."""
    from beom_workbench.core import workbranch

    root = Path(target_root)
    if not root.is_dir():
        return None
    hubs, agents, docs = [], [], []

    def rel(p: Path) -> str:
        return p.relative_to(root).as_posix()

    def scan(d: Path, depth: int) -> None:
        for n in AGENT_FILES:
            if (d / n).is_file():
                agents.append(rel(d / n))
        if depth == 0:
            return
        try:
            subs = sorted(x for x in d.iterdir() if x.is_dir() and x.name not in SKIP_DIRS and not x.name.startswith("."))
        except OSError:
            return
        for x in subs:
            if RULE_DIR.search(x.name):
                readme = next((x / n for n in ("README.md", "readme.md", "README.ko.md", "INDEX.md") if (x / n).is_file()), None)
                if readme and (x / ".git").exists() or readme and depth == 2:
                    hubs.append(rel(readme))
                elif any(x.glob("*.md")) or any(x.glob("*.pdf")) or any(x.glob("*.docx")):
                    docs.append(rel(x) + "/")
            scan(x, depth - 1)

    scan(root, 2)
    repos = []
    if not workbranch.repo_top(root):
        for r in workbranch.child_repos(root)[:40]:
            st = sorted({name for marker, name in STACK_MARKERS.items()
                         if (r / marker).is_file() or (marker.startswith("*") and any(r.glob(marker)))})
            repos.append({"name": rel(r), "stack": st})
    out = {"hubs": hubs[:6], "agents": agents[:10], "docs": docs[:10], "repos": repos}
    return out if any(out.values()) else None


def _guides_lines(g: dict | None) -> list[str]:
    if not g:
        return []
    lines = []
    parts = []
    if g.get("hubs"):
        parts.append("규칙 허브: " + ", ".join(g["hubs"]) + " (작업 유형별 읽기 순서가 있으면 그대로 따른다)")
    if g.get("agents"):
        parts.append("에이전트 지시: " + ", ".join(g["agents"]) + " (그 폴더에서 작업할 때)")
    if g.get("docs"):
        parts.append("정책·가이드·화면 설계 문서: " + ", ".join(g["docs"]))
    if parts:
        lines.append("[작업 규칙·정책 — 대화창에 붙이지 않았어도 대상 폴더 안에 있으니 시작 전에 직접 읽고 따른다] " + " · ".join(parts))
    if g.get("repos"):
        lines.append("[작업 공간의 저장소] " + ", ".join(f"{r['name']}({'/'.join(r['stack']) or '?'})" for r in g["repos"])
                     + " — 요청에 맞는 저장소를 여기서 찾아 그 안에서 작업한다 (요청이 가리키는 저장소가 분명하지 않으면 묻는다)")
    return lines


def _existing_line(ex: dict) -> str:
    parts = [f"파일 {ex['files']}개"]
    if ex.get("stack"):
        parts.append("스택: " + ", ".join(ex["stack"]))
    if ex.get("docs"):
        parts.append("문서: " + ", ".join(ex["docs"][:8]))
    if ex.get("screens"):
        parts.append("화면: " + ", ".join(ex["screens"][:8]))
    return ("[대상 폴더의 기존 자산 — 작업을 만들 때 이미 있던 것] " + " · ".join(parts)
            + " — 새로 만들기 전에 언어·업무 규칙·화면 톤앤매너를 먼저 파악해 맞춘다 (기존 자산 기반)")


def ui_lang(settings: Settings) -> str:
    """앱 화면 언어 코드 (설정 → 운영체제 언어 → 한국어)."""
    from beom_workbench import i18n
    from beom_workbench.core import storage
    try:
        return i18n.resolve(storage.load_prefs(settings).get("lang") or "auto")
    except Exception:  # noqa: BLE001
        return i18n.DEFAULT


def lang_of(text: str | None) -> str | None:
    """사용자 요청의 언어 — 알 수 없으면 None (첨부·링크·코드뿐인 요청)."""
    t = text or ""
    if re.search(r"[가-힣]", t):
        return "한국어"
    if re.search(r"[\u3040-\u30ff]", t):
        return "日本語"
    if re.search(r"[\u4e00-\u9fff]", t):
        return "中文"
    # 코드·링크·경로·첨부 안내는 빼고 본다 — 남은 영어 낱말이 거의 없으면 요청 언어를 알 수 없다
    prose = re.sub(r"`[^`]*`|https?://\S+|[A-Za-z]:[\\/]\S*|\S*[\\/]\S+|\.beom\S*", " ", t)
    return "English" if len(re.findall(r"[A-Za-z]{2,}", prose)) >= 3 else None


def agent_language_setting(agent: str | None) -> str | None:
    """사용자가 에이전트 자체 설정에 정해 둔 응답 언어 (Claude Code settings.json 의 "language") — 있으면 그것을 따른다."""
    if (agent or "claude") != "claude":
        return None
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        lang = json.loads((base / "settings.json").read_text(encoding="utf-8")).get("language")
    except (OSError, ValueError, AttributeError):
        return None
    return (str(lang).strip() or None) if lang else None


ATTACH_HEADS = ("첨부한 기획서대로 만든다.", "첨부한 파일을 보고 반영한다.")


def user_request(run: dict) -> str:
    """사용자가 직접 적은 요청 — 앱이 붙인 첨부·링크 안내(한국어)는 뺀다 (언어 판단·다시 하기에 쓴다)."""
    o = run.get("origin")
    if isinstance(o, dict) and "request" in o:
        return o.get("request") or ""
    text = run.get("request") or ""
    for mark in ("\n\n[첨부 문서 — ", "\n\n[링크 — ", "\n\n[이 메시지의 첨부 — "):
        text = text.split(mark, 1)[0]
    text = text.strip()
    return "" if text in ATTACH_HEADS else text


def lang_line(run: dict, text: str | None, what: str) -> str | None:
    """에이전트에게 줄 언어 지시 한 줄. 기본은 에이전트 자체 규칙(사용자 설정)을 따른다 —
    우리 지시문이 한국어라, 요청 언어가 분명할 때만 그 언어로 쓰라고 적는다 (영어 요청이 한국어로 끌려가지 않게).
    요청 언어를 알 수 없거나(첨부·링크뿐) 사용자가 에이전트 설정에 언어를 정해 두었으면 적지 않는다."""
    lang = lang_of(text)
    if not lang or agent_language_setting(run.get("agent")):
        return None
    return f"[언어] {lang} — {what}"


MODE_REF = ("레퍼런스 기반 — 원본 저장소의 기능·동작을 참고해 대상 프로젝트에 맞게 옮기거나(포팅) 새로 만든다. 사용자 요청이 "
            "'참고해 새로·우리 것 기준으로'면 원본과 똑같이 맞추기보다 요청과 대상 프로젝트(기존 엔진·API·규칙·화면 톤)를 따르고, "
            "원본에서는 설계·동작 방식을 가져온다 — 합격 기준도 요청에서 정한다")


def _brief(run: dict, st: dict | None) -> str:
    lines = [x for x in [lang_line(run, user_request(run) or run.get('feature'), "중간 메시지·보고·질문 모두"),
             f"[모드] {'요청사항 기반 만들기' if run['mode'] == 'request' else MODE_REF}",
             f"[대상 폴더] {run['target_root']}"] if x]
    if run.get("request"):
        lines.append(f"[사용자 요청]\n{run['request']}")
    if run["mode"] == "reference":
        lines.append(f"[원본 프로젝트] {run.get('source_project_id') or run.get('project_id')} (원본 분석용 wb_* 도구의 project_id)")
        if run.get("feature"):
            lines.append(f"[옮길 기능] {run['feature']}")
        if run.get("source_run"):
            lines.append(f"[원본 실행 정보] {run['source_run']}")
    if run.get("context_project"):
        lines.append(f"[넣을 저장소 분석] project_id={run['context_project']} — 기존 구조·패턴을 먼저 보고 맞춘다")
    if run.get("existing"):
        lines.append(_existing_line(run["existing"]))
    lines += _guides_lines(run.get("guides"))
    from beom_workbench.core import prereqs
    pre = prereqs.brief_line(prereqs.needs(run["target_root"]))
    if pre:
        lines.append(pre)
    if st:
        lines.append(f"[원장] project_id={run['project_id']} port_id={run['port_id']}")
        lines.append(f"[다음 할 일] {st['next_step']}")
        unmet = [k for k, v in st["gates"].items() if not v]
        if unmet:
            lines.append("[미충족 조건] " + " / ".join(unmet))
        last = st.get("differential") or {}
        if last.get("why"):
            lines.append(f"[검증 상태] {last['why']}")
    elif run["mode"] == "reference":
        lines.append("[다음 할 일] wb_features·wb_port_slice 로 옮길 범위를 찾고 wb_port_create 로 원장을 만든다")
        if not run.get("feature") and not run.get("request"):
            lines.append("[옮길 기능 미정] 사용자가 기능을 정하지 않았다 — wb_features 로 기능 목록을 정리해 "
                         "wb_person_ask 의 choices 로 고르게 한 뒤 진행한다")
    plan = run.get("plan") or {}
    if plan.get("steps"):
        done = sum(1 for x in plan["steps"] if x.get("status") == "done")
        lines.append(f"[계획] {'승인됨' if plan.get('approved') else '승인 대기'} · {done}/{len(plan['steps'])} 단계 완료")
    msgs = [m for m in run.get("messages") or [] if not m.get("delivered")]
    if msgs:
        lines.append("[사용자 추가 요청 — 이번 라운드에 반영]\n" + "\n".join(f"- {m['text']}" for m in msgs))
    return "\n".join(lines)


# ---------------------------------------------------------------- 실행

_COMMON_DIRS = {"desktop", "바탕 화면", "바탕화면", "documents", "문서", "downloads", "다운로드", "onedrive", "pictures", "사진",
                "music", "음악", "videos", "동영상"}


def work_folder(target_root: str, name: str | None, mode: str) -> str:
    """바탕 화면·문서·다운로드·홈·드라이브 맨 위처럼 개인 파일이 섞인 공용 폴더를 고르면 그 안에 작업 이름으로 새 폴더를 만든다.
    (그대로 쓰면 개인 문서가 '기존 자산'으로 잡히고, .oss·결과물이 바탕 화면에 흩어진다.)"""
    if mode == "free" or not target_root:
        return target_root
    p = Path(target_root).expanduser()
    try:
        r = p.resolve()
    except OSError:
        return target_root
    home = Path.home().resolve()
    common = r == home or r.parent == r or r.name.lower() in _COMMON_DIRS or (
        r.parent == home and r.name.lower().startswith("onedrive"))
    if not common:
        return target_root
    base = re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|]+', " ", (name or "새 작업"))).strip().strip(".")[:40].strip() or "새 작업"
    cand, n = r / base, 2
    while cand.exists() and any(cand.iterdir()):
        cand, n = r / f"{base} {n}", n + 1
    return str(cand)


_ACTIVE = ("running", "waiting_answer", "waiting_approval", "waiting_plan", "waiting_limit")


def _repo_busy(settings: Settings, target_root: str) -> str | None:
    """같은 저장소(또는 작업 공간)를 작업 브랜치로 쓰며 지금 돌고 있는 다른 작업의 이름."""
    top = workbranch.repo_top(target_root)
    mine = os.path.normcase(str(Path(top or target_root).resolve()))
    for r in inbox.list_runs(settings):
        wb = r.get("work_branch") or {}
        if not wb or r.get("status") not in _ACTIVE or lock_owner(settings, r["id"]) is None:
            continue
        paths = {os.path.normcase(str(Path(x["repo"]).resolve())) for x in wb.get("repos") or []}
        if wb.get("workspace"):
            paths.add(os.path.normcase(str(Path(wb["workspace"]).resolve())))
        if any(mine == p or mine.startswith(p.rstrip("\\/") + os.sep) or p.startswith(mine.rstrip("\\/") + os.sep) for p in paths):
            return r.get("name") or r["id"]
    return None


def create(settings: Settings, *, mode: str, target_root: str, request: str = "", name: str | None = None,
           source: str | None = None, feature: str | None = None, source_run: str | None = None,
           context_project: str | None = None, max_rounds: int = 0, model: str | None = None,
           auth: str = "subscription", policy: dict | None = None, agent: str = "claude",
           plan_approval: bool = True, effort: str | None = None, confirm_at: str = "start",
           new_branch: bool = True, sandbox: bool = True) -> dict:
    """실행 준비 — 요청사항 모드는 원장까지 만들고, 오픈소스 모드는 원본 저장소를 열어 둔다(인벤토리는 첫 단계에서)."""
    if mode not in ("request", "reference", "plan", "free"):
        raise ToolError("invalid_request", "mode 는 request · reference(레퍼런스) · plan(기획) · free(자유 대화)")
    target_root = work_folder(target_root, name, mode)
    Path(target_root).mkdir(parents=True, exist_ok=True)
    # 기존 git 저장소면 새 브랜치를 만들어 그 안에서만 작업한다 (사용자가 끄지 않았다면). 저장소가 아니면(새 프로젝트) 그대로.
    # 브랜치를 먼저 옮기고 그 내용을 본다 — 기존 자산·규칙 문서는 작업할 브랜치(main 기준)의 것이어야 한다
    wb_new = None
    if new_branch:
        busy = _repo_busy(settings, target_root)
        if busy:
            raise ToolError("conflict", f"같은 저장소에서 돌고 있는 작업이 있습니다 ({busy}) — 두 작업이 브랜치를 번갈아 바꾸게 됩니다",
                            recovery="그 작업을 멈추거나 끝낸 뒤 시작하세요 (또는 '새 브랜치에서 작업'을 끄고 지금 브랜치에서)")
        wb_new = workbranch.start(target_root, name, secrets.token_hex(3))
        if not Path(target_root).is_dir():
            raise ToolError("not_found", f"main 에서 만든 작업 브랜치에는 이 폴더가 없습니다: {target_root}",
                            recovery="저장소 맨 위 폴더를 고르거나 '새 브랜치에서 작업'을 끄세요")
    existing = existing_assets(target_root)
    meta = {"existing": existing, "guides": guides(target_root), "mode": mode, "target_root": str(Path(target_root).resolve()), "request": request.strip(), "name": name or "project",
            "feature": feature, "source_run": source_run, "context_project": context_project, "max_rounds": max_rounds,
            "model": model, "auth": auth, "project_id": None, "port_id": None, "session_id": None, "policy": policy or {},
            "agent": agent, "plan_approval": plan_approval, "messages": [], "effort": effort or None,
            # start: 처음에 기준과 계획을 한 번에 확인하고 끝까지 / end: 멈추지 않고 만든 뒤 마지막에 한 번 확인
            "confirm_at": confirm_at if confirm_at in ("start", "end") else "start", "sandbox": bool(sandbox)}
    agents_mod.get(agent)  # 모르는 에이전트면 여기서 거절
    if mode in ("plan", "free"):
        if not request.strip():
            raise ToolError("invalid_request", "무엇을 할지 적어 주세요")
    elif mode == "request":
        if not request.strip():
            raise ToolError("invalid_request", "요청사항이 비어 있음")
        made = make.create(settings, name=meta["name"], request=request, target_root=target_root, context=context_project,
                           author="runner")
        meta.update(project_id=made["project_id"], port_id=made["port_id"])
    else:
        if not source:
            raise ToolError("invalid_request", "원본 저장소(source)가 필요함")
        opened = project.open_repo(settings, source, None, name)
        meta.update(project_id=opened["project"]["id"], source=source)
    meta["work_branch"] = wb_new
    run = inbox.new_run(settings, meta)
    wb = run.get("work_branch")
    if wb and wb.get("workspace"):
        inbox.log(settings, run["id"], "note", tone="",
                  text=f"여러 저장소가 든 작업 공간입니다 — 에이전트가 고친 저장소마다 작업 브랜치 {wb['branch']} 를 만들어 그 안에서만 작업하고 "
                       "라운드마다 그 브랜치에 커밋합니다 (손대지 않은 저장소는 그대로).")
    elif wb:
        r0 = wb["repos"][0]
        carried = f" 커밋하지 않은 변경 {r0['carried']}개는 첫 커밋 '작업 전 변경'으로 따로 담았습니다." if r0.get("carried") else ""
        if r0.get("stashed"):
            carried += (f" 원래 브랜치에서 하던 커밋 안 된 변경은 develop 의 실험이라 가져오지 않고 stash 에 보관했습니다"
                        f" — 그 브랜치로 돌아가 git stash pop 으로 되살립니다 (\"{r0['stashed']}\").")
        inbox.log(settings, run["id"], "note", tone="" if r0.get("from_main") else "warn",
                  text=f"작업 브랜치 {wb['branch']} 를 {r0.get('base') or '지금 커밋'}에서 만들었습니다"
                       f"{'' if r0.get('from_main') else ' (main 이 없거나 가져가다 충돌해 지금 브랜치에서)'}. 이 브랜치에서만 작업하고 "
                       f"라운드마다 이 브랜치에 커밋합니다.{carried}")
    elif not new_branch and (workbranch.repo_top(target_root) or workbranch.child_repos(target_root)):
        inbox.log(settings, run["id"], "note", tone="warn", text="'새 브랜치에서 작업'을 꺼서 지금 브랜치에서 그대로 작업합니다")
    return run


def recount_cost(settings: Settings, run_id: str) -> float | None:
    """예전 실행기가 누적값을 다시 더해 부풀린 비용을 기록(events)으로 다시 센다 — 세션마다 마지막 누적값의 합. 한 번만."""
    run = inbox.load_run(settings, run_id)
    if run.get("cost_recounted") or run.get("cost_sessions") is not None or not run.get("cost_usd"):
        return None
    if not getattr(agents_mod.AGENTS.get(run.get("agent", "claude")), "cost_cumulative", False):
        return None
    sessions: dict[str, float] = {}
    cur = None
    for e in inbox.events(settings, run_id)[0]:
        if e.get("kind") == "agent_init" and e.get("session_id"):
            cur = e["session_id"]
        elif e.get("kind") == "agent_result" and e.get("cost_usd") and cur:
            sessions[cur] = max(sessions.get(cur, 0.0), float(e["cost_usd"]))
    total = round(sum(sessions.values()), 4)
    if not sessions or total >= float(run["cost_usd"]):
        inbox.update_run(settings, run_id, lambda r: r.update(cost_recounted=True))
        return None
    inbox.update_run(settings, run_id, lambda r: r.update(cost_usd=total, cost_sessions=sessions, cost_recounted=True,
                                                          cost_base=min(float(r.get("cost_base") or 0), total)))
    return total


def status(settings: Settings, run: dict, fp_max_age: float = 0) -> dict | None:
    if not run.get("port_id"):
        if run.get("project_id"):
            conn, _ = project.load(settings, run["project_id"])
            ports = port.list_(conn)["items"]
            if ports:
                run["port_id"] = ports[-1]["port_id"]
                inbox.update_run(settings, run["id"], lambda r: r.update(port_id=run["port_id"]))
        if not run.get("port_id"):
            return None
    conn, _ = project.load(settings, run["project_id"])
    return port.status_(conn, run["port_id"], brief=True, fp_max_age=fp_max_age)


def _mtime(p: Path) -> int:
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return 0


# 끝난 작업에 사용자가 추가 요청을 보낸 첫 라운드 — 처음부터 다시 분석·기획하지 않고, 정해 둔 기준 위에서 고친 뒤 다시 검증한다
FOLLOW_UP = ("\n\n이번 라운드: 끝난 작업에 사용자가 위 [사용자 추가 요청]을 보냈다 — 요청대로 고친다. 처음부터 다시 분석·기획하지 않는다. "
             "승인된 요구사항 안의 수정이면 바뀐 동작을 확인하는 합격 케이스를 고치거나 더하고(wb_port_capture) 다시 검증한다(wb_port_verify). "
             "요청이 요구사항·범위를 바꾸면 요구사항·합격 케이스를 먼저 고쳐 사용자 확인을 받은 뒤 구현한다. "
             "사용자 확인은 한 번에 받는다 — 바뀌는 요구사항·케이스를 구현 전에 모두 정해 함께 올리고, 검증하다 케이스를 더 고쳐야 하면 "
             "하나씩 올리지 말고 모아서 한 번에 올린다 (확인 카드가 여러 번 뜨지 않게). 끝나면 인수인계(wb_handoff)를 새로 남긴다.")

# 인계까지 끝난 기획에 고칠 점이 왔다 — 반영할 것이 없으면(이미 들어 있으면) 되묻지 말고 그렇다고 인계를 다시 남긴다
PLAN_FOLLOW_UP = ("\n\n[이번 라운드] 인계까지 끝난 기획에 사용자가 위 내용을 보냈다. 보낸 것만 관련 문서 모두에 맞춰 반영하고 다시 점검한 뒤 "
                  "wb_handoff 로 인계를 새로 남긴다. 이미 기획에 들어 있어 바꿀 것이 없으면 문서는 그대로 두고, 어디에 이미 있는지를 "
                  "인계 summary 에 적어 다시 인계한다 — 무엇을 할지 되묻지 않는다.")

LOCK_FILE = "runner.lock"
WATCHDOG_SEC = 15
LOCK_BEAT_SEC = 10
LOCK_STALE_SEC = 45
# 아래 기본값은 사용자가 라운드 규칙(core/rounds.py, 앱의 실행 옵션)으로 바꿀 수 있다 — 러너는 rounds.get() 을 읽는다
STALL_ROUNDS = 3   # 이만큼 연속으로 기록·계획·대상 폴더가 그대로면 멈춘다
HARD_ROUNDS = 100  # '제한 없음'이어도 이 이상은 돌지 않는다 (비용 안전장치) — 메시지를 보내면 이어 간다
# 에이전트가 이만큼 아무 출력이 없고 사용자에게 묻는 중도 아니면 이번 라운드를 끝낸다 (멈춘 프로세스가 영원히 붙잡지 않게)
ROUND_IDLE_SEC = int(os.environ.get("BEOM_WORKBENCH_ROUND_IDLE", "1800"))
STOP_FILE = "stop.request"

# ---------------------------------------------------------------- 라운드 실패 분류
# 에이전트가 오류로 끝난 라운드는 진전 없음(헛돎)과 다르다. 한도·일시 장애·로그인·대화 길이 초과를 구분해
# 라운드를 쓰지 않고 기다리거나, 다시 시도하거나, 사람에게 넘긴다 (세 번 헛돌고 '확인 필요'로 멈추지 않게).
_AUTH = re.compile(r"not logged in|please run /login|run `?\w+ (auth )?login|log ?in required|invalid (api|x-api)[ _-]?key|"
                   r"authentication[_ ](error|failed)|unauthori[sz]ed|\b401\b|(oauth )?token (has )?(expired|revoked)|"
                   r"re-?authenticate|credentials? (are )?(missing|invalid)", re.I)
_LIMIT = re.compile(r"usage limit|rate[ _-]?limit|too many requests|\b429\b|quota|limit reached|hit your (usage )?limit|"
                    r"resource[_ ]exhausted|out of (credits|extra usage)|credit balance|weekly limit|session limit", re.I)
_CONTEXT = re.compile(r"prompt is too long|context[_ ](window|length)|maximum context|too many tokens|context_length_exceeded|"
                      r"input is too long", re.I)
_TRANSIENT = re.compile(r"overloaded|\b5(00|02|03|04|29)\b|internal server error|bad gateway|service unavailable|gateway timeout|"
                        r"timed? ?out|econnreset|etimedout|enotfound|eai_again|network error|connection (reset|refused|error|closed)|"
                        r"stream (error|disconnected)|socket hang up|temporarily unavailable|reconnecting|exceeded retry limit", re.I)
LIMIT_BACKOFF = (300, 600, 1200, 1800, 3600)     # 한도 해제 시각을 모를 때 기다리는 시간 (초), 이후로는 마지막 값
TRANSIENT_BACKOFF = (30, 60, 120, 300, 600)      # 일시 장애 재시도 간격 — 횟수는 라운드 규칙 transient_retries, 이후로는 마지막 값
HARD_FAIL_ROUNDS = 2                             # 출력도 도구 사용도 없이 실패한 라운드가 이만큼 이어지면 멈춘다


def classify_failure(text: str) -> str | None:
    """라운드 오류 문장 → auth | limit | context | transient | None (모름)."""
    if not text:
        return None
    for kind, rx in (("auth", _AUTH), ("context", _CONTEXT), ("limit", _LIMIT), ("transient", _TRANSIENT)):
        if rx.search(text):
            return kind
    return None


def parse_reset(text: str, now: datetime | None = None) -> datetime | None:
    """한도 메시지에서 풀리는 시각 — 'limit reached|1731600000', 'try again in 2 hours 13 minutes',
    'resets 3pm', 'try again at 3:04 PM', 'retry after 30 seconds'. 모르면 None. 결과는 지역 시각(naive)."""
    now = now or datetime.now()
    m = re.search(r"\|\s*(\d{10})\b", text)
    if m:
        return datetime.fromtimestamp(int(m.group(1)))
    m = re.search(r"(?:in|after)\s+((?:\d+(?:\.\d+)?\s*(?:days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b[\s,]*(?:and\s+)?)+)",
                  text, re.I)
    if m:
        secs = 0.0
        for n, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]+)", m.group(1), re.I):
            u = unit.lower()
            secs += float(n) * (86400 if u.startswith("d") else 3600 if u.startswith("h") else 1 if u.startswith("s") else 60)
        if secs > 0:
            return now + timedelta(seconds=secs)
    m = re.search(r"(?:resets?|try again|available)\s*(?:at|on)?\s*(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?", text, re.I)
    if m and (m.group(3) or m.group(2)):
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        ap = (m.group(3) or "").lower().replace(".", "")
        if ap == "pm" and hour < 12:
            hour += 12
        if ap == "am" and hour == 12:
            hour = 0
        if hour < 24 and minute < 60:
            at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            return at if at > now else at + timedelta(days=1)
    return None


def request_stop(settings: Settings, run_id: str) -> None:
    """다른 프로세스에서 돌고 있는 실행기에 멈춤을 요청한다 (1초 안에 알아챈다)."""
    (inbox.run_dir(settings, run_id) / STOP_FILE).write_text(inbox._now(), encoding="utf-8")


def lock_owner(settings: Settings, run_id: str) -> dict | None:
    """이 실행을 지금 진행 중인 실행기 — 앱 스레드든 CLI 든 다른 앱 창이든. 심장 박동이 끊기면 없는 것으로 본다."""
    p = inbox.run_dir(settings, run_id) / LOCK_FILE
    try:
        if time.time() - p.stat().st_mtime > LOCK_STALE_SEC:
            return None
    except OSError:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}  # 막 만들어져 아직 비어 있는 잠금 — 주인이 있다


# Windows 에서 창 없는 앱이 콘솔 프로그램을 띄우면 명령마다 검은 창이 뜬다 — 숨긴 콘솔을 주면 자식(node·powershell)도 그것을 물려받는다
# Windows: 창 없이 / macOS·Linux: 새 프로세스 그룹으로 — 멈출 때 에이전트가 띄운 자식(node·셸·개발 서버)까지 한 번에 끝내게
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {"start_new_session": True}

# 에이전트가 이 도구를 부르면 지금 하는 일 한 줄을 툴킷이 바꾼다 — 에이전트가 알리지 않아도 '요구사항 정리 중'이 남지 않게
AUTO_ACTIVITY = {"wb_make_requirements": "요구사항을 정리했습니다 — 합격 기준을 세우는 중",
                 "wb_port_capture": "합격 기준을 정리했습니다 — 계획을 세우는 중",
                 "wb_plan_set": "기준과 계획을 정리했습니다",
                 "wb_port_verify": "합격 기준으로 검증하는 중",
                 "wb_handoff": "인수인계를 정리하는 중",
                 "wb_brief_check": "기획 패키지 점검 중 (문서끼리 맞물림)",
                 "wb_brief_screens": "화면 설계 스크린샷 찍는 중"}


def kill_tree(proc: subprocess.Popen) -> None:
    """에이전트 프로세스와 그 자식까지 끝낸다 — Windows 의 .cmd 실행 파일은 terminate 로 껍데기만 죽고 node 가 남는다."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=30)
        return
    import signal
    try:  # start_new_session 으로 띄웠다 — 그룹 전체에 TERM, 3초 뒤에도 남으면 KILL
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()


class Runner:
    """한 실행을 라운드 단위로 진행한다. 앱은 스레드로, CLI 는 그대로 돌린다."""

    def __init__(self, settings: Settings, run_id: str, exe=None):
        self.settings = settings
        self.run_id = run_id
        self.agent = agents_mod.get(inbox.load_run(settings, run_id).get("agent"), exe)
        self.exe = self.agent.exe
        self._stop = threading.Event()
        self.proc: subprocess.Popen | None = None
        self._token = f"{os.getpid()}-{id(self)}-{time.time_ns()}"
        self._released = threading.Event()

    def stop(self) -> None:
        self._stop.set()
        if self.proc:
            kill_tree(self.proc)

    def _acquire(self) -> None:
        p = inbox.run_dir(self.settings, self.run_id) / LOCK_FILE
        body = json.dumps({"token": self._token, "pid": os.getpid(), "at": inbox._now()}).encode("utf-8")
        for _ in range(2):
            try:
                fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if lock_owner(self.settings, self.run_id) is not None:
                    raise ToolError("conflict", "이 실행은 이미 다른 곳(앱 창 또는 CLI)에서 진행 중입니다",
                                    recovery="그쪽에서 멈춘 뒤 이어서 진행하세요") from None
                p.unlink(missing_ok=True)  # 심장 박동이 끊긴 잠금 — 앞선 실행기가 비정상 종료
                continue
            os.write(fd, body)
            os.close(fd)
            break
        else:
            raise ToolError("conflict", "실행 잠금을 잡지 못했습니다")

        stop_req = p.with_name(STOP_FILE)
        stop_req.unlink(missing_ok=True)

        def beat():
            n = 0
            while not self._released.wait(1.0):
                try:
                    if stop_req.exists():  # 다른 프로세스(앱 창)의 멈춤 요청
                        stop_req.unlink(missing_ok=True)
                        self.stop()
                except Exception:  # noqa: BLE001 — 멈춤 처리가 실패해도 심장 박동은 계속 (잠금이 끊긴 것으로 보이지 않게)
                    pass
                n += 1
                if n % LOCK_BEAT_SEC == 0:
                    try:
                        os.utime(p)
                    except FileNotFoundError:
                        return
                    except OSError:
                        pass

        threading.Thread(target=beat, daemon=True).start()

    def _release(self) -> None:
        self._released.set()
        p = inbox.run_dir(self.settings, self.run_id) / LOCK_FILE
        try:
            if json.loads(p.read_text(encoding="utf-8")).get("token") == self._token:
                p.unlink()
        except (OSError, ValueError):
            pass

    def _set(self, **kw) -> dict:
        return inbox.update_run(self.settings, self.run_id, lambda r: r.update(kw))

    def _rules(self, run: dict) -> str:
        rules = {"free": FREE_RULES, "plan": PLAN_RULES}.get(run.get("mode"), RULES)
        from beom_workbench.core import sandbox
        if run.get("agent", "claude") == "claude" and sandbox.active(run):
            rules += "\n\n" + sandbox.rule(run)
        if run.get("agent") == "codex" and sys.platform == "win32":
            rules += "\n\n" + CODEX_WIN_RULE
        wb = run.get("work_branch")
        if wb:
            where = (f"여러 저장소 작업 공간 — 고친 저장소마다 라운드가 끝나면 앱이 같은 이름의 브랜치({wb['branch']})를 지금 커밋에서 "
                     "만들어 변경을 옮긴다. 고칠 저장소가 main 이 아닌 브랜치에 있으면 고치기 전에 wb_person_ask 로 확인한다"
                     if wb.get("workspace") else f"git 브랜치 {wb['branch']} 에서만 일한다 (원래 브랜치는 건드리지 않는다)")
            rules += (f"\n\n[작업 브랜치] {where}. "
                      "다른 브랜치로 바꾸기(switch·checkout <브랜치>)·병합·pull·rebase·cherry-pick·reset --hard·브랜치 삭제·push 는 "
                      "하지 않는다 — 규칙으로 막혀 있다 (사용자가 요청하면 직접 하라고 안내한다). "
                      "커밋은 하지 않아도 된다 — 라운드가 끝나면 앱이 작업 브랜치에 커밋한다. 합치기는 사용자가 한다 (흐름: 작업 브랜치를 "
                      "develop 에 합쳐 시험하고, 적용은 작업 브랜치를 main 에 합친다. develop 은 main·작업 브랜치에 합치지 않는다) — "
                      "끝낼 때 작업 브랜치 이름과 합치기 전에 볼 점만 알린다.")
        return rules

    def _keep_branch(self, run: dict) -> None:
        """작업 브랜치를 벗어났으면 되돌린다 (규칙을 거치지 않는 에이전트·스크립트가 바꿨을 때). 못 되돌리면 실행을 멈춘다."""
        wb = run.get("work_branch")
        if not wb or inbox.decide(run, "branch") == "allow":
            return
        try:
            notes = workbranch.ensure(wb)
        except (ToolError, OSError, subprocess.SubprocessError) as e:
            inbox.log(self.settings, self.run_id, "note", tone="warn", text=f"{e} — 작업을 멈춥니다. 브랜치를 정리한 뒤 이어 가세요"[:400])
            self._stop.set()
            return
        for msg in notes:
            inbox.log(self.settings, self.run_id, "note", tone="warn", text=msg)

    def _adopt_repos(self, run: dict) -> dict:
        """작업 공간에서 이번 라운드에 고친 저장소에 작업 브랜치를 만든다 (변경을 함께 옮김)."""
        wb = run.get("work_branch")
        if not wb or not wb.get("workspace"):
            return run
        try:
            made = workbranch.adopt(wb)
        except (ToolError, OSError, subprocess.SubprocessError) as e:
            inbox.log(self.settings, self.run_id, "note", tone="warn", text=f"{e}"[:400])
            return run
        if not made:
            return run
        ok = [m for m in made if not m.get("skipped")]
        skipped = [m for m in made if m.get("skipped")]

        def upd(r):
            r["work_branch"].setdefault("repos", []).extend(ok)
            r["work_branch"].setdefault("skipped", []).extend(m["repo"] for m in skipped)

        run = inbox.update_run(self.settings, self.run_id, upd)
        for m in ok:
            inbox.log(self.settings, self.run_id, "note", tone="" if m.get("from_main") else "warn",
                      text=f"{m['name']}: 작업 브랜치 {m['branch']} 를 만들어 이번 변경을 옮겼습니다 ({m.get('base') or '지금 커밋'}에서"
                           f"{'' if m.get('from_main') else ' — main 이 아닌 브랜치였습니다'})")
        for m in skipped:  # develop 위에서 고쳤는데 main 기반으로 못 옮김 — develop 에서 feature 를 만들지 않는다
            inbox.log(self.settings, self.run_id, "note", tone="warn",
                      text=f"{m['name']}: {m['reason']} — 변경은 develop 에 커밋하지 않은 채 그대로 둡니다. main 에서 feature 를 만들어 직접 옮기세요")
        return run

    def _commit_round(self, run: dict) -> None:
        """라운드에서 바뀐 것을 작업 브랜치에 커밋한다. 훅이 막으면 한 번만 알리고 변경은 그대로 둔다."""
        wb = run.get("work_branch")
        if not wb:
            return
        try:
            done = workbranch.commit_round(wb, f"beom-workbench: {run.get('name') or '작업'} — 라운드 {run['round']}\n\nrun {self.run_id}")
        except (ToolError, OSError, subprocess.SubprocessError) as e:
            if not run.get("branch_commit_warned"):
                self._set(branch_commit_warned=True)
                inbox.log(self.settings, self.run_id, "note", tone="warn",
                          text=f"작업 브랜치에 커밋하지 못했습니다 — 변경은 {wb['branch']} 에 커밋하지 않은 채 있습니다. {e}"[:400])
            return
        for repo, sha in done:
            inbox.log(self.settings, self.run_id, "branch_commit", branch=wb["branch"], repo=repo, sha=sha, round=run["round"])

    def _turn(self, run: dict, prompt: str) -> dict:
        self._keep_branch(run)
        if self._stop.is_set():  # 작업 브랜치로 돌아가지 못했다 — 에이전트를 돌리지 않는다
            return {}
        snap = integrity.snapshot(self.settings, run) if integrity.IN_APP["on"] else None
        try:
            return self._turn_agent(run, prompt)
        finally:
            if snap is not None:
                found = integrity.changes(self.settings, self.run_id, snap)
                if found:  # 앱을 거치지 않은 사람 결정 — 되돌리고 멈춘다
                    integrity.restore(self.settings, self.run_id, snap, found)
                    self._stop.set()
            run = inbox.load_run(self.settings, self.run_id)
            self._keep_branch(run)
            run = self._adopt_repos(run)
            self._commit_round(run)

    def _turn_agent(self, run: dict, prompt: str) -> dict:
        rules = self._rules(run)
        argv, env = self.agent.command(self.settings, run, prompt, rules)
        from beom_workbench.core import prereqs
        env = {**env, **prereqs.agent_env()}  # 설치된 OpenSSL 을 Rust 빌드가 찾게 (Windows)
        inbox.log(self.settings, self.run_id, "round_start", round=run["round"], agent=self.agent.id, prompt=prompt[:3000])
        self._set(activity=None)  # 지난 라운드의 '승인 대기' 같은 한 줄이 새 라운드에 남지 않게 — 에이전트가 새로 알릴 때까지는 '라운드 N 진행 중'
        feed_stdin = getattr(self.agent, "stdin_prompt", False)
        # stderr 는 파일로 — 파이프로 두면 에이전트가 로그를 많이 쓸 때 양쪽이 서로 기다리며 멈춘다
        err_file = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(argv, cwd=run["target_root"], env=env,
                                     stdin=subprocess.PIPE if feed_stdin else subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=err_file, **NO_WINDOW)
        try:  # PID 가 다시 쓰여도 다른 프로그램을 에이전트로 보지 않게 시작 시각도 남긴다
            import psutil
            ctime = psutil.Process(self.proc.pid).create_time()
        except Exception:  # noqa: BLE001
            ctime = None
        self._set(agent_pid=self.proc.pid, agent_ctime=ctime)
        if feed_stdin:
            try:
                self.proc.stdin.write(self.agent.stdin_text(run, prompt, rules).encode("utf-8"))
                self.proc.stdin.close()
            except OSError:
                pass
        result = {}
        texts: list[str] = []
        self.tools_used = 0
        last = {"t": time.monotonic()}
        proc = self.proc

        def watchdog():
            worked, tick = 0.0, time.monotonic()
            while proc.poll() is None:
                time.sleep(WATCHDOG_SEC)
                now, rules = time.monotonic(), self._loop_rules()
                waiting = bool(inbox.pending(self.settings, self.run_id))
                if not waiting:  # 사람 답을 기다린 시간은 라운드 시간에 넣지 않는다
                    worked += now - tick
                tick = now
                idle = rules["round_idle_min"] * 60
                if now - last["t"] > idle and not waiting:
                    inbox.log(self.settings, self.run_id, "note", tone="warn",
                              text=f"에이전트가 {idle // 60}분 동안 아무 출력이 없어 이번 라운드를 끝냈습니다")
                    kill_tree(proc)
                    return
                if worked > rules["round_max_min"] * 60 and not waiting:
                    inbox.log(self.settings, self.run_id, "note", tone="warn",
                              text=f"라운드가 {rules['round_max_min']}분을 넘겨 끝냈습니다 — 다음 라운드에서 이어 갑니다")
                    kill_tree(proc)
                    return

        threading.Thread(target=watchdog, daemon=True).start()
        stream = {"chars": 0, "tool": None, "thinking": False, "t": 0.0}
        for raw in self.proc.stdout:
            last["t"] = time.monotonic()
            try:
                ev = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            for e in self.agent.parse(ev):
                kind = e.pop("kind")
                if kind == "progress":  # 에이전트가 쓰는 중 — 몇 초마다 한 번만 기록한다
                    if e.get("tool"):
                        stream.update(tool=e["tool"], chars=0)
                    stream["chars"] += e.get("chars", 0)
                    stream["thinking"] = bool(e.get("thinking"))
                    if time.monotonic() - stream["t"] > 3:
                        stream["t"] = time.monotonic()
                        self._set(streaming={"chars": stream["chars"], "tool": stream["tool"], "thinking": stream["thinking"],
                                             "at": inbox._now()})
                    continue
                if kind in ("text", "tool", "result") and stream["chars"]:
                    stream.update(chars=0, tool=None)
                    self._set(streaming=None)
                if kind == "init":
                    inbox.log(self.settings, self.run_id, "agent_init", **e)
                elif kind == "text":
                    texts.append(e["text"])
                    inbox.log(self.settings, self.run_id, "agent", items=[{"text": e["text"]}])
                elif kind == "tool":
                    self.tools_used += 1
                    inbox.log(self.settings, self.run_id, "agent",
                              items=[{"tool": e.get("tool"), "input": mask_command(json.dumps(e.get("input"), ensure_ascii=False)[:300])}])
                    short = str(e.get("tool") or "").split("__")[-1]
                    if short in AUTO_ACTIVITY:
                        self._set(activity={"text": AUTO_ACTIVITY[short], "at": inbox._now()})
                    if short in ("wb_make_requirements", "wb_port_capture") and run.get("auto_approve_round") == run.get("round"):
                        self._auto_approve()
                elif kind == "result":
                    result = {"is_error": not e.get("ok", True), **e}
                    inbox.log(self.settings, self.run_id, "agent_result", subtype=e.get("subtype"), is_error=not e.get("ok", True),
                              cost_usd=e.get("cost_usd"), turns=e.get("turns"), usage=e.get("usage"), result=e.get("text"))
                    u = e.get("usage")
                    if not e.get("cost_usd") and isinstance(u, dict):
                        # 비용을 알려 주지 않는 에이전트(Codex 등)는 쓴 토큰을 센다 — 화면에 사용량이 비어 보이지 않게
                        # 이어 받은 세션은 그 세션 전체 누적값을 알려 준다 — 세션마다 마지막 값만 두고 합한다 (비용과 같은 방식)
                        n = int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)
                        sid = e.get("session_id") or run.get("session_id") or f"round-{run.get('round')}"
                        if n:
                            def put(r, n=n, sid=sid):
                                per = r.setdefault("token_sessions", {})
                                per[sid] = max(int(per.get(sid) or 0), n)
                                r["tokens"] = sum(per.values())
                            run = inbox.update_run(self.settings, self.run_id, put)
                if e.get("session_id"):
                    run = self._set(session_id=e["session_id"])
                    if e.get("cost_usd"):
                        self._add_cost(e["session_id"], float(e["cost_usd"]))
            if self._stop.is_set():
                break
        if self._stop.is_set():
            kill_tree(self.proc)
        code = self.proc.wait()
        if stream["chars"] or stream["tool"]:
            self._set(streaming=None)
        err_file.seek(0, os.SEEK_END)
        err_file.seek(max(0, err_file.tell() - 6000))
        err = err_file.read().decode("utf-8", "replace")[-1500:]
        err_file.close()
        if code != 0 and not result:
            inbox.log(self.settings, self.run_id, "agent_error", exit_code=code, stderr=err)
        if self._stop.is_set():
            return result
        if code != 0 and not result:
            result = {"is_error": True, "subtype": "exit", "text": ""}
        # 실패 분류용 — 결과 문장 + 마지막 출력 + stderr (어느 쪽에 한도·로그인 문구가 오는지는 에이전트마다 다르다)
        result["_why"] = "\n".join(x for x in (result.get("text") or "", (texts[-1] if texts else ""), err) if x)[-4000:]
        result["_exit"] = code
        result["_tools"] = self.tools_used
        return result

    def _auto_approve(self) -> None:
        """사용자가 처음 확인 때 바꾼 결정을 반영한 요구사항·케이스 변경 — 사용자 결정의 결과라 다시 묻지 않고 승인한다 (그 라운드만).
        도구 결과가 원장에 쓰인 뒤에 보도록 잠깐 기다렸다 본다."""
        def work():
            time.sleep(3)
            try:
                run = inbox.load_run(self.settings, self.run_id)
                conn, _ = project.load(self.settings, run["project_id"])
                reviews = [h for h in port.pending_reviews(conn, run["port_id"])
                           if h["kind"] in ("requirements", "expectation", "case_change")]
                if reviews:
                    integrity.record(self.run_id, "reviews")
                    port_guard.approve(conn, run["port_id"], reviews, channel="decision-change", approved_by="사용자 결정 반영(자동)")
                    inbox.log(self.settings, self.run_id, "approved", items=[r["item_id"] for r in reviews], auto=True)
            except (ToolError, OSError, KeyError) as e:
                inbox.log(self.settings, self.run_id, "note", tone="warn", text=f"결정 변경 반영 자동 승인을 못 했습니다 — {e}"[:300])

        threading.Thread(target=work, daemon=True).start()

    def _checkpoint(self, run: dict, label: str) -> None:
        try:
            cid = Checkpoints(inbox.run_dir(self.settings, self.run_id), run["target_root"]).snapshot(label)
            if cid:
                inbox.log(self.settings, self.run_id, "checkpoint", id=cid, label=label)
        except (ToolError, OSError) as e:
            inbox.log(self.settings, self.run_id, "checkpoint_error", message=str(e)[:300])

    def _spec_settled(self, run: dict) -> bool:
        """요구사항·합격 기준(또는 오픈소스 모드의 원본 기록)이 승인까지 끝났는지 — 계획 단계로 넘어갈 조건."""
        st = status(self.settings, run)
        if not st:
            return False
        if run["mode"] == "request":
            req = st.get("requirements") or {}
            return bool(req.get("total")) and st["gates"].get(port.GATE_REQ_CASES, False) and not any(
                h["kind"] in ("requirements", "expectation", "case_change") for h in
                port.pending_reviews(project.load(self.settings, run["project_id"])[0], run["port_id"]))
        return st["counts"].get("pending", 0) == 0 and st.get("last_check") is not None

    def run(self) -> dict:
        if not self.exe:
            raise ToolError("not_found", f"{self.agent.name} CLI 를 찾지 못함", recovery=self.agent.status().get("hint"))
        tam = inbox.load_run(self.settings, self.run_id).get("tampered")
        if tam:
            raise ToolError("denied", "승인·규칙이 앱 밖에서 바뀌어 멈춘 작업입니다 — " + " / ".join(tam.get("what") or []),
                            recovery="바뀐 내용을 확인하고 앱의 '확인했음'을 누른 뒤 이어 가세요")
        self._acquire()
        try:
            return self._run()
        except BaseException:
            run = inbox.load_run(self.settings, self.run_id)
            if run.get("status") in ("running", "waiting_answer", "waiting_approval", "waiting_plan", "waiting_limit"):
                self._set(status="stopped")
            raise
        finally:
            if self.proc:
                kill_tree(self.proc)
            self._release()

    def _run(self) -> dict:
        run = self._set(status="running")
        if run["round"] == 0:
            self._checkpoint(run, "시작 전")
        if run["mode"] in ("free", "plan"):
            return self._run_simple()
        stalls = 0
        finish_tries = 0
        while not self._stop.is_set():
            run = inbox.load_run(self.settings, self.run_id)
            st = status(self.settings, run)
            if st and st.get("finished") and st["finished"].get("valid") and run.get("handoff"):
                return self._finish()
            if st and st["complete"] and not (st.get("finished") or {}).get("valid") and finish_tries < 2:
                finish_tries += 1  # 판정이 바로 무효가 되면(대상 폴더가 계속 바뀜) 헛돌지 않고 라운드로 넘긴다
                conn, _ = project.load(self.settings, run["project_id"])
                port.finish(conn, run["port_id"], "runner")
                inbox.log(self.settings, self.run_id, "finished", gates=st["gates"])
                continue
            if self._answered():
                # 승인을 기다리는 동안 질문에 답이 왔다 — 승인 전에 답을 기준·계획에 반영하는 라운드를 먼저 돈다
                # (답을 반영하지 않은 기준을 승인하게 하고, 반영 뒤에 또 멈추지 않게)
                if not self._settle_orphans():
                    break
                continue
            waiting = st and port.pending_reviews(project.load(self.settings, run["project_id"])[0], run["port_id"])
            end_mode = run.get("confirm_at", "start") == "end"
            only_human_left = bool(st) and all(v for k, v in st["gates"].items() if k != port.GATE_HUMAN)
            plan = run.get("plan") or {}
            plan_needed = run.get("plan_approval", True) and not end_mode
            # 완료 조건을 다 채운 뒤에 나온 계획은 기다리지 않는다 (끝난 일에 대한 계획 — 인계로 넘어간다)
            plan_pending = bool(plan_needed and plan.get("steps") and not plan.get("approved")) and not (st and st["complete"])
            spec_ready = bool(waiting) or self._spec_settled(run)  # 기준을 저장했다 (승인 전이든 후든)
            # 처음에 한 번: 기준(+계획)이 모두 나오면 한 번 멈춰 함께 승인받고, 그 뒤로는 끝까지 간다
            # 마지막에 한 번: 승인 말고 다른 조건이 다 채워졌을 때만 멈춘다
            # 멈춘 동안 사용자가 고칠 점을 보내면 승인하지 않은 채로 반영하는 라운드를 돈다
            if run["round"] > 0 and not self._new_messages():
                gate_wait = bool(waiting) and "승인" in st["next_step"]
                stop = (gate_wait and only_human_left) if end_mode else (
                    (gate_wait or plan_pending) and spec_ready and (not plan_needed or bool(plan.get("steps"))))
                # 레퍼런스: 원본 라이선스 확인만 남았다 — 에이전트는 대신 못 하므로 라운드를 쓰지 않고 사람의 확인을 기다린다
                license_wait = bool(st) and not st["gates"].get(port.GATE_LICENSE, True) and all(
                    v for k, v in st["gates"].items() if k != port.GATE_LICENSE)
                stop = stop or license_wait
                if stop:
                    self._set(status="waiting_approval")
                    inbox.log(self.settings, self.run_id, "waiting_approval", items=len(waiting or []) + int(plan_pending))

                    seen: dict = {}

                    def settled():
                        # 승인을 기다리는 동안(몇 시간일 수도) 매초 원장 전체를 읽지 않게 — run.json·원장 파일이 그대로면 지난 답
                        ws = project.workspace(self.settings, run["project_id"])
                        stamp = tuple(_mtime(f) for f in (inbox.run_dir(self.settings, self.run_id) / "run.json",
                                                          ws / "ledger.db", ws / "ledger.db-wal"))
                        if seen.get("stamp") == stamp:
                            return seen["value"]
                        seen["stamp"], seen["value"] = stamp, _settled()
                        return seen["value"]

                    def _settled():
                        r = inbox.load_run(self.settings, self.run_id)
                        p = r.get("plan") or {}
                        need = r.get("plan_approval", True) and r.get("confirm_at", "start") != "end"
                        open_plan = need and p.get("steps") and not p.get("approved")
                        conn_ = project.load(self.settings, r["project_id"])[0]
                        if port.pending_reviews(conn_, r["port_id"]) or open_plan:
                            return False
                        lic = (port.status_(conn_, r["port_id"], brief=True).get("license") or {}) if license_wait else {}
                        return not lic or bool(lic.get("acked"))

                    if not self._wait(lambda: self._new_messages() or settled() or self._answered()):
                        break
                    run = self._set(status="running")
                    continue
            if inbox.pending(self.settings, self.run_id):
                if not self._settle_orphans():
                    break
                continue
            if (run.get("max_rounds") and run["round"] >= run["max_rounds"]) or run["round"] >= self._loop_rules()["hard_rounds"]:  # 0 = 제한 없음 (헛돌면 따로 멈춘다)
                inbox.log(self.settings, self.run_id, "max_rounds", round=run["round"])
                return self._set(status="stopped_max_rounds")
            if self._over_budget(run):
                return self._set(status="stopped_max_rounds")
            self._keep_branch(run)  # 라운드·메시지를 쓰기 전에 — 작업 브랜치로 못 돌아가면 그대로 멈춘다
            if self._stop.is_set():
                break
            run = self._set(round=run["round"] + 1)
            prompt = _brief(run, st)
            revising = self._new_messages() and (waiting or ((run.get("plan") or {}).get("steps")
                                                           and not (run.get("plan") or {}).get("approved")))
            if revising:
                prompt += ("\n\n이번 라운드: 사용자가 승인하지 않고 고칠 점을 보냈다. 위 요청대로 요구사항·합격 케이스"
                           "(다시 wb_port_capture) 또는 계획(wb_plan_set)을 고치고 끝낸다. 승인은 사람만 한다.")
            elif self._new_messages() and st and st["complete"]:
                # 끝난(완료 조건을 다 채운) 작업에 추가 요청이 왔다 — '인수인계만, 코드는 바꾸지 않는다' 지시와 부딪히지 않게 먼저 본다
                prompt += FOLLOW_UP
            elif run["round"] == 1 and run["mode"] == "request" and end_mode:
                prompt += ("\n\n이번 라운드: 요청을 요구사항(wb_make_requirements)으로 나누고, 요구사항마다 합격 케이스 파일을 "
                           "대상 폴더의 .beom/cases.json 에 써서 wb_port_capture 로 저장한 뒤, 승인을 기다리지 말고 계획(wb_plan_set)·"
                           "구현·검증(wb_port_map, wb_port_verify)까지 이어서 한다. 사용자는 마지막에 한 번 확인한다. "
                           "진행을 막는 질문만 하고, 나머지는 합리적으로 가정한 뒤 wb_activity 로 '가정: …'을 남긴다.")
            elif run["round"] == 1 and run["mode"] == "request":
                prompt += ("\n\n이번 라운드: 요청을 요구사항(wb_make_requirements)으로 나누고, 요구사항마다 합격 케이스 파일을 "
                           "대상 폴더의 .beom/cases.json 에 써서 wb_port_capture 로 저장한 뒤, 구현 계획(wb_plan_set)까지 낸다 — "
                           "사전 설계 결정(decisions: 형태·데이터 저장·스택·실행 방법·디자인 방향)을 함께. "
                           "사용자는 기준·계획·결정을 처음에 한 번에 확인하고, 그 뒤로는 끝날 때까지 맡긴다. "
                           "추천으로 정할 수 없는 것만 지금 한 번에 묻는다. "
                           + ("구현은 사용자 승인 뒤에 한다." if run.get("plan_approval", True) else "계획 없이 바로 구현해도 된다 — 기준 승인 뒤에."))
            elif end_mode and not (st and st["complete"]) and not only_human_left:
                prompt += ("\n\n이번 라운드: 사용자 확인은 마지막에 한 번 받는다 — 승인을 기다리지 말고 남은 구현·검증을 진행해 "
                           "승인 말고 다른 완료 조건을 모두 채운다. 진행을 막는 질문만 한다.")
            elif st and st["complete"] and not run.get("handoff"):
                # 완료 조건을 다 채웠으면 계획이 없었더라도 인계로 — 끝난 일에 계획을 새로 내고 승인을 기다리지 않게
                prompt += "\n\n이번 라운드: 완료 조건을 모두 채웠다. wb_handoff 로 인수인계를 남긴다 (코드는 바꾸지 않는다)."
            elif (self._spec_settled(run) or (waiting and plan_needed)) and not (run.get("plan") or {}).get("steps"):
                prompt += "\n\n이번 라운드: 구현 계획을 wb_plan_set 으로 낸다 (단계·바꿀 파일·실행할 명령). 코드는 아직 바꾸지 않는다."
            sent = [m for m in run.get("messages") or [] if not m.get("delivered")]
            self._mark_delivered([(m.get("at"), m.get("text")) for m in sent])
            finish_tries = 0
            before = self._progress_sig(run)
            result = self._turn(run, prompt)
            run = inbox.load_run(self.settings, self.run_id)
            self._checkpoint(run, f"라운드 {run['round']}")
            handled = self._after_failure(result, sent)
            if handled == "stop":
                return inbox.load_run(self.settings, self.run_id)
            if handled == "retry":
                continue  # 라운드를 쓰지 않은 것으로 되돌렸다 — 진전 판단에도 넣지 않는다
            # 진전 판단은 기록·계획·대상 폴더가 바뀌었는지로만 (활동 알림·읽기만 한 라운드는 진전이 아니다)
            if self._progress_sig(run) == before and not inbox.pending(self.settings, self.run_id):
                stalls += 1
                # 인수인계를 남긴 뒤 진전이 없으면 바로 멈춘다 — 남은 일이 사용자 쪽(설치·결정)에 막혀 같은 보고만 되풀이하게 된다
                if stalls >= (1 if run.get("handoff") else self._loop_rules()["stall_rounds"]):
                    inbox.log(self.settings, self.run_id, "stalled", round=run["round"], rounds=stalls, handoff=bool(run.get("handoff")))
                    return self._set(status="stopped_max_rounds", stalled=True)
            else:
                stalls = 0
        return self._set(status="stopped")

    def _run_simple(self) -> dict:
        """자유 대화·기획: 원장·완료 조건 없이 — 메시지마다 한 번 돌고(자유), 기획서를 남기면 끝(기획)."""
        stalls = 0
        while not self._stop.is_set():
            run = inbox.load_run(self.settings, self.run_id)
            if run["mode"] == "plan" and run.get("handoff") and not self._new_messages():
                return self._finish()
            if inbox.pending(self.settings, self.run_id):
                if not self._settle_orphans():
                    break
                continue
            if run["mode"] == "free" and run["round"] > 0 and not self._new_messages():
                self._set(status="waiting_input")
                if not self._wait(self._new_messages):
                    break
                self._set(status="running")
                continue
            if run.get("max_rounds") and run["round"] >= run["max_rounds"]:
                inbox.log(self.settings, self.run_id, "max_rounds", round=run["round"])
                return self._set(status="stopped_max_rounds")
            if self._over_budget(run):
                return self._set(status="stopped_max_rounds")
            self._keep_branch(run)  # 라운드·메시지를 쓰기 전에 — 작업 브랜치로 못 돌아가면 그대로 멈춘다
            if self._stop.is_set():
                break
            run = self._set(round=run["round"] + 1)
            msgs = [m for m in run.get("messages") or [] if not m.get("delivered")]
            if run["round"] == 1:
                prompt = run.get("request") or ""
                if msgs:
                    prompt += "\n\n" + "\n".join(m["text"] for m in msgs)
                if run["mode"] == "plan":
                    head = lang_line(run, user_request(run), "문서·질문·중간 메시지 모두")
                    prompt = (head + "\n" if head else "") + f"[기획할 것]\n{prompt}\n\n[대상 폴더] {run['target_root']}"
                    if run.get("existing"):
                        prompt += "\n" + _existing_line(run["existing"])
                    for line in _guides_lines(run.get("guides")):
                        prompt += "\n" + line
            else:
                prompt = "\n\n".join(m["text"] for m in msgs) or "이어서 진행한다."
                if run["mode"] == "plan" and run.get("revision") and msgs:
                    prompt += PLAN_FOLLOW_UP
            self._mark_delivered([(m.get("at"), m.get("text")) for m in msgs])
            before = self._progress_sig(run)
            result = self._turn(run, prompt)
            run = inbox.load_run(self.settings, self.run_id)
            self._checkpoint(run, f"라운드 {run['round']}")
            handled = self._after_failure(result, msgs)
            if handled == "stop":
                return inbox.load_run(self.settings, self.run_id)
            if handled == "retry":
                continue
            if run["mode"] == "plan" and self._progress_sig(run) == before:
                stalls += 1
                if stalls >= self._loop_rules()["stall_rounds"]:
                    inbox.log(self.settings, self.run_id, "stalled", round=run["round"], rounds=stalls)
                    return self._set(status="stopped_max_rounds", stalled=True)
            else:
                stalls = 0
        return self._set(status="stopped")

    def _after_failure(self, result: dict, sent: list[dict] | None = None) -> str | None:
        """오류로 끝난 라운드 처리. "retry" = 라운드를 되돌리고 다시, "stop" = 멈춤(상태 기록됨), None = 보통 라운드."""
        if self._stop.is_set():
            return None
        if not result.get("is_error") or result.get("subtype") in ("error_max_turns",):
            self._fails = {}
            return None
        why = result.get("_why") or ""
        kind = classify_failure(why)
        n = getattr(self, "_fails", {}).get(kind, 0) + 1
        self._fails = {kind: n}  # 다른 종류가 끼면 횟수는 처음부터
        line = next((x.strip() for x in why.splitlines() if x.strip() and classify_failure(x) == kind), None) \
            or next((x.strip() for x in why.splitlines() if x.strip()), "")
        line = line[:300]
        inbox.log(self.settings, self.run_id, "round_error", subtype=result.get("subtype"), cause=kind, text=line)

        def undo_round():  # 이번 라운드는 없었던 것으로 — 실었던 사용자 메시지도 다음 라운드에 다시 싣는다
            keys = {(m.get("at"), m.get("text")) for m in sent or []}

            def put(r):
                r["round"] = max(0, int(r.get("round") or 0) - 1)
                for m in r.get("messages") or []:
                    if (m.get("at"), m.get("text")) in keys:
                        m["delivered"] = False
            inbox.update_run(self.settings, self.run_id, put)

        if kind == "auth":
            hint = None
            try:
                hint = self.agent.status().get("hint")
            except Exception:  # noqa: BLE001 — 안내 문구를 못 만들어도 멈춤은 기록한다
                pass
            inbox.log(self.settings, self.run_id, "auth_needed", agent=self.agent.name, text=line, hint=hint)
            undo_round()
            self._set(status="needs_login")
            return "stop"
        if kind == "context":
            undo_round()
            if n > 1:  # 새 대화로도 안 된다 — 지시 자체가 너무 길다
                inbox.log(self.settings, self.run_id, "note", tone="fail", text="새 대화로 시작해도 입력이 너무 깁니다 — 첨부·요청을 줄여 주세요")
                self._set(status="stopped")
                return "stop"
            self._set(session_id=None)
            inbox.log(self.settings, self.run_id, "note", tone="warn",
                      text="대화가 에이전트의 기억 한도를 넘어 새 대화로 이어 갑니다 — 진행 상황은 원장에서 다시 읽습니다")
            return "retry"
        if kind == "limit":
            undo_round()
            reset = parse_reset(why)
            wait = (reset - datetime.now()).total_seconds() + 60 if reset else LIMIT_BACKOFF[min(n, len(LIMIT_BACKOFF)) - 1]
            wait = max(60.0, min(wait, 7 * 86400))
            until = (datetime.now() + timedelta(seconds=wait)).astimezone()
            rules = self._loop_rules()
            waited = getattr(self, "_limit_waited", 0.0) if n > 1 else 0.0  # 한도로 이어서 기다린 시간 (풀린 줄 알았는데 또 걸린 경우 포함)
            cap = rules["limit_wait_max_min"] * 60
            if rules["on_limit"] == "stop" or wait > cap or waited + wait > cap:
                why_stop = ("라운드 규칙이 '한도에 걸리면 멈춤'입니다" if rules["on_limit"] == "stop"
                            else f"풀리는 시각이 대기 상한({rules['limit_wait_max_min']}분)보다 늦습니다" if wait > cap
                            else f"이어서 기다린 시간이 대기 상한({rules['limit_wait_max_min']}분)을 넘습니다 — 주간 한도처럼 오래 걸리는 한도일 수 있습니다")
                inbox.log(self.settings, self.run_id, "limit_wait", agent=self.agent.name, text=line, known=bool(reset),
                          until=until.isoformat(timespec="seconds"), seconds=int(wait), stopped=True)
                inbox.log(self.settings, self.run_id, "note", tone="warn",
                          text=f"{self.agent.name} 사용 한도에 걸려 멈췄습니다 — {why_stop}. {until.strftime('%H:%M')} 이후 이어서 진행하세요")
                self._set(status="stopped", limit_until=until.isoformat(timespec="seconds"))
                return "stop"
            self._limit_waited = waited + wait
            self._set(status="waiting_limit", limit_until=until.isoformat(timespec="seconds"))
            inbox.log(self.settings, self.run_id, "limit_wait", agent=self.agent.name, text=line, known=bool(reset),
                      until=until.isoformat(timespec="seconds"), seconds=int(wait))
            # 기다리는 동안 사용자가 메시지를 보내면 (한도가 풀렸다고 보고) 바로 다시 시도한다
            if not self._wait(lambda: datetime.now().astimezone() >= until or self._new_messages()):
                self._set(status="stopped", limit_until=None)
                return "stop"
            self._set(status="running", limit_until=None)
            return "retry"
        if kind == "transient":
            undo_round()
            if n > self._loop_rules()["transient_retries"]:
                inbox.log(self.settings, self.run_id, "note", tone="fail",
                          text=f"{self.agent.name} 연결 오류가 {n}번 이어져 멈췄습니다 — 네트워크·서비스 상태를 확인한 뒤 이어서 진행하세요")
                self._set(status="error", error=line)
                return "stop"
            wait = TRANSIENT_BACKOFF[min(n, len(TRANSIENT_BACKOFF)) - 1]
            inbox.log(self.settings, self.run_id, "retry_wait", text=line, seconds=wait, attempt=n)
            if self._stop.wait(wait):
                self._set(status="stopped")
                return "stop"
            return "retry"
        # 모르는 오류 — 아무 일도 못 하고 끝난 라운드가 이어지면 같은 실패를 되풀이하는 것이다
        if not result.get("_tools") and n >= self._loop_rules()["fail_rounds"]:
            self._set(status="error", error=line or f"exit {result.get('_exit')}")
            inbox.log(self.settings, self.run_id, "note", tone="fail",
                      text=f"{self.agent.name} 가 {n}번 연속 바로 실패해 멈췄습니다: {line or '출력 없음'}")
            return "stop"
        return None

    def _finish(self) -> dict:
        """끝 — 완료 판정과 인수인계가 있다. 에이전트가 '진행 중'으로 둔 채 넘어간 계획 단계는 끝난 것으로 표시한다
        (완료 조건을 다 채웠으니 하던 단계도 끝났다 — 끝난 작업에 '진행 중'이 남아 보이지 않게)."""
        def close(r):
            for st in (r.get("plan") or {}).get("steps") or []:
                if st.get("status") == "doing":
                    st["status"] = "done"
            r["status"] = "done"

        return inbox.update_run(self.settings, self.run_id, close)

    def _add_cost(self, session_id: str, reported: float) -> None:
        """라운드 비용을 더한다. Claude Code 는 이어 받은 세션의 누적값을 알려 주므로 지난번 값과의 차이만 더한다
        (그대로 더하면 라운드가 쌓일수록 비용이 부풀고 비용 상한이 일찍 걸린다)."""
        cumulative = getattr(self.agent, "cost_cumulative", False)

        def put(r):
            seen = r.setdefault("cost_sessions", {})
            last = float(seen.get(session_id) or 0) if cumulative else 0.0
            delta = reported - last if reported >= last else reported  # 줄었으면 새 세션으로 본다
            seen[session_id] = reported
            r["cost_usd"] = round(float(r.get("cost_usd") or 0) + delta, 4)

        inbox.update_run(self.settings, self.run_id, put)

    def _over_budget(self, run: dict) -> bool:
        """비용 상한 — 사람이 '이어서'를 누른 뒤부터 다시 센다(cost_base)."""
        cap = self._loop_rules().get("cost_budget_usd") or 0
        spent = float(run.get("cost_usd") or 0) - float(run.get("cost_base") or 0)
        if not cap or spent < cap:
            return False
        inbox.log(self.settings, self.run_id, "note", tone="warn",
                  text=f"비용 상한 ${cap} 에 닿아 멈췄습니다 (이번에 ${spent:.2f}) — 이어서 진행하면 다시 ${cap} 까지 씁니다")
        return True

    def _loop_rules(self) -> dict:
        """사용자가 정한 라운드 규칙 — 매번 읽으므로 도는 중에 바꿔도 다음 판단부터 반영된다."""
        try:
            return rounds.get(self.settings)
        except Exception:  # noqa: BLE001 — 설정 파일이 깨져도 실행은 기본값으로 계속
            return rounds.defaults()

    def _progress_sig(self, run: dict) -> str:
        """라운드 사이에 무언가 진행됐는지 — 원장 기록 끝, 계획·인수인계, 대상 폴더 스냅샷."""
        try:
            head = None
            if run.get("project_id") and run.get("port_id"):
                conn, _ = project.load(self.settings, run["project_id"])
                head = conn.execute("SELECT log_head FROM port WHERE id = ?", (run["port_id"],)).fetchone()
        except ToolError:
            head = None
        cps = Checkpoints(inbox.run_dir(self.settings, self.run_id), run["target_root"]).list()
        return json.dumps([head[0] if head else None, run.get("plan"), run.get("handoff"), cps[0]["id"] if cps else None],
                          ensure_ascii=False, default=str)

    def _settle_orphans(self) -> bool:
        """라운드가 끝났는데 남은 질문·권한 요청 — 묻던 에이전트는 이미 끝났다 (Codex 는 답을 기다리지 않고 턴을 닫기도 한다).

        사용자 답을 기다렸다가 다음 라운드 지시에 실어 보낸다. 허용된 명령은 이 실행에서 기억해 다시 시도하면 통과한다.
        """
        d = inbox.run_dir(self.settings, self.run_id) / "inbox"
        left = inbox.pending(self.settings, self.run_id)
        if not left:
            return True
        self._set(status="waiting_answer")
        inbox.log(self.settings, self.run_id, "waiting_answer", items=len(left))
        if not self._wait(lambda: all((d / f"{q['id']}.answer.json").exists() or not (d / f"{q['id']}.json").exists()
                                      for q in left)):
            return False
        add, cmds = [], []
        for q in left:
            ans = inbox.take_answer(d / f"{q['id']}.answer.json")
            if ans is None:
                continue  # 다른 쪽(살아 있던 에이전트)이 이미 받아 갔다
            (d / f"{q['id']}.json").unlink(missing_ok=True)
            inbox.log(self.settings, self.run_id, "answer", id=q["id"], answer=ans)
            if q.get("kind") == "question":
                text = ans.get("text") or ans.get("choice") or ""
                add.append({"text": f"앞서 한 질문에 대한 답 — 질문: {q.get('question', '')[:1000]} / 답: {text[:3000]}",
                            "at": inbox._now(), "delivered": False, "answer_to": q["id"]})
            else:
                inp = q.get("input") or {}
                if ans.get("allow") and str(q.get("tool", "")).startswith("mcp:"):
                    cmds.extend(inp.get("commands") or ([inp["command"]] if inp.get("command") else []))
                verdict = "허용 — 같은 작업을 다시 시도한다" if ans.get("allow") else "거절 — 다른 방법을 찾는다"
                add.append({"text": f"앞서 한 권한 요청에 대한 답: {q.get('summary', '')[:500]} → {verdict}",
                            "at": inbox._now(), "delivered": False, "answer_to": q["id"]})

        def put(r):
            r.setdefault("messages", []).extend(add)
            r["approved_cmds"] = list(dict.fromkeys((r.get("approved_cmds") or []) + cmds))
            r["status"] = "running"

        inbox.update_run(self.settings, self.run_id, put)
        return True

    def _answered(self) -> bool:
        """남아 있는 질문 중 사용자가 답한 것이 있나 (묻던 에이전트는 이미 라운드를 끝냈다)."""
        d = inbox.run_dir(self.settings, self.run_id) / "inbox"
        return any(q.get("kind") == "question" and (d / f"{q['id']}.answer.json").exists()
                   for q in inbox.pending(self.settings, self.run_id))

    def _new_messages(self) -> bool:
        """사용자가 새로 보낸 메시지가 있나 — 앱에서 직접 고쳤다는 알림(from=edit)은 깨우지 않고 다음 라운드에 함께 전한다."""
        return any(not m.get("delivered") and m.get("from") != "edit"
                   for m in inbox.load_run(self.settings, self.run_id).get("messages") or [])

    def _mark_delivered(self, sent: list[tuple]) -> None:
        """이번 지시에 실은 메시지만 전달됨으로 — 지시를 만든 뒤 들어온 메시지는 다음 라운드로 넘긴다."""
        if not sent:
            return

        def mark(r):
            for m in r.get("messages") or []:
                if not m.get("delivered") and (m.get("at"), m.get("text")) in sent:
                    m["delivered"] = True

        inbox.update_run(self.settings, self.run_id, mark)

    def _wait(self, ready) -> bool:
        while not self._stop.wait(1.0):
            if ready():
                return True
        return False

