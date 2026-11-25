"""Independent review before handoff — a reviewer with a fresh context reads only this run's diff with a checklist for the
languages it touches, and reports the bugs it is confident about. The building agent reviews its own work with the same blind
spots it wrote it with; a second reader that never saw the conversation does not share them.

The reviewer is the run's own agent CLI (same sign-in, same model), started read-only in the toolkit's own agent home, so it
leaves nothing in the user's history and cannot change files."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

# 언어별로 '이 diff 에서 실제로 잘 터지는 것'만 — 길게 늘어놓으면 리뷰어가 사소한 것까지 올린다
CHECKS = {
    "python": "변경 가능한 기본값 인자, 예외를 삼키는 except, 닫지 않는 파일·연결, 정수/실수 나눗셈·반올림, 시간대 없는 datetime, "
              "SQL·셸 문자열 이어 붙이기, 경로 조작(../), 반복 중 컬렉션 변경, None 확인 누락",
    "javascript": "== 와 ===, await 누락·처리 안 한 Promise 거절, 클로저의 오래된 상태, innerHTML 에 사용자 값(XSS), 이벤트 리스너 중복 등록, "
                  "parseInt 기수·Number 변환 NaN, 날짜의 월(0부터)·시간대, localStorage JSON.parse 실패, 배열을 제자리에서 바꾸는 sort·splice",
    "typescript": "any·as 로 덮은 타입 오류, 선택 속성 확인 누락, await 누락, 좁혀지지 않은 유니온, == 와 ===, innerHTML XSS",
    "java": "equals 대신 ==, null 확인 누락, 닫지 않는 자원(try-with-resources), 트랜잭션 경계, 동시성(공유 가변 상태), 문자열 SQL 이어 붙이기, "
            "BigDecimal 대신 double 로 돈 계산",
    "kotlin": "!! 남용, 플랫폼 타입 null, 코루틴 취소·예외 전파, 공유 가변 상태",
    "go": "무시한 err, nil 맵 쓰기, 루프 변수 캡처, 고루틴 누수·채널 닫기, defer 순서, 경쟁 상태",
    "rust": "unwrap·expect 로 넘긴 오류 경로, 정수 넘침, 잠금 순서·교착, unsafe 근거",
    "csharp": "async void, await 누락, IDisposable 미해제, null 참조, 문자열 SQL",
    "web": "레이블 없는 입력, 키보드로 닿지 않는 동작, 폼 제출 기본 동작, 서버 응답 실패 처리 누락, 사용자 값의 HTML 삽입",
    "sql": "WHERE 없는 UPDATE·DELETE, 인덱스 없는 조인 조건, NULL 비교(= NULL), 문자열로 만든 쿼리",
}
EXT = {".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript",
       ".tsx": "typescript", ".vue": "javascript", ".svelte": "javascript", ".java": "java", ".kt": "kotlin", ".go": "go", ".rs": "rust",
       ".cs": "csharp", ".html": "web", ".jsp": "web", ".sql": "sql", ".xml": "sql"}
MAX_DIFF = 60_000
TIMEOUT = 420

PROMPT = """너는 이 저장소를 처음 보는 시니어 리뷰어다. 아래 diff 는 다른 개발자가 방금 만든 변경이다. 이 변경이 들여온 결함만 찾는다.

<scope>
- 볼 것: diff 의 추가·수정된 줄과, 그 줄이 부르는·그 줄을 부르는 코드 (필요하면 Read·Grep 으로 저장소 파일을 열어 확인한다).
- 언어별로 특히 볼 것:
{checks}
- 공통: 요청과 다른 동작, 경계값(0·빈 값·최대값·음수), 오류 경로, 보안(인젝션·XSS·비밀 노출), 데이터 손실.
</scope>

<do_not_report>
- 이름·형식·주석·취향 같은 스타일 (포매터와 린터가 따로 본다).
- 이번 diff 가 건드리지 않은 곳에 원래 있던 문제.
- "~하면 더 좋다" 수준의 제안, 테스트를 더 쓰라는 일반론.
- 이 프로젝트를 쓰는 사람의 보통 환경에서는 일어나지 않는 경우 — 특이한 표준 출력 인코딩 설정, '-' 로 시작하는 파일 이름·FIFO·장치 파일,
  호출자가 내부 함수를 엉뚱하게 부르는 경우 등. 요청이 그런 환경을 말했을 때만 본다 (드문 경우를 막느라 코드가 부풀면 그것이 더 큰 문제다).
- 확신이 0.8 미만인 것 — 지적이 틀리면 개발자가 그것을 고치느라 시간을 버린다. 확실한 것 몇 개가 애매한 것 여러 개보다 낫다.
</do_not_report>

<output>
JSON 하나만 출력한다: {{"findings": [{{"severity": "high|medium|low", "file": "상대 경로", "line": 줄번호, "problem": "무엇이 언제 틀리는지 한 문장 (구체적인 입력·상황)", "fix": "고치는 방법 한 문장", "confidence": 0.0~1.0}}]}}
high = 이 프로젝트의 사용자가 보통 쓰다 보면 틀린 결과·오류·데이터 손실·보안 문제를 만난다. medium = 보통 쓰는 범위 안의 드문 조건에서만.
low = 거의 영향 없음.
결함이 없으면 {{"findings": []}}. 문장은 한국어로.
</output>

<example>
{{"findings": [{{"severity": "high", "file": "app.js", "line": 42, "problem": "남은 자리가 0인 모임도 신청 버튼이 눌려 자리가 -1 이 된다", "fix": "신청 전에 seats > 0 을 확인하고 0 이면 버튼을 끈다", "confidence": 0.92}}]}}
</example>

<diff>
{diff}
</diff>"""


def languages(files: list[str]) -> list[str]:
    seen = []
    for f in files:
        lang = EXT.get(Path(f).suffix.lower())
        if lang and lang not in seen:
            seen.append(lang)
    return seen


def build_prompt(diff: str, files: list[str]) -> str:
    langs = languages(files) or ["javascript"]
    checks = "\n".join(f"  · {lang}: {CHECKS[lang]}" for lang in langs if lang in CHECKS)
    return PROMPT.format(checks=checks, diff=diff[:MAX_DIFF])


def parse(text: str) -> list[dict]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for f in data.get("findings") or []:
        if not isinstance(f, dict):
            continue
        try:
            conf = float(f.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        sev = str(f.get("severity") or "low").lower()
        if conf < 0.8 or sev not in ("high", "medium", "low"):
            continue
        out.append({"severity": sev, "file": str(f.get("file") or "")[:200], "line": f.get("line"),
                    "problem": str(f.get("problem") or "")[:400], "fix": str(f.get("fix") or "")[:300], "confidence": round(conf, 2)})
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(out, key=lambda x: order[x["severity"]])[:12]


def _argv(settings, run: dict, prompt: str, out_file: Path) -> tuple[list[str], dict, str | None]:
    """리뷰어 명령 — 실행의 에이전트 그대로, 읽기만. 표준 입력으로 줄 글(Codex)과 함께."""
    from beom_workbench.core import agenthome, agents
    agent_id = run.get("agent") or "claude"
    agent = agents.get(agent_id)
    env = agent.env(run)
    model = run.get("model") or ""
    if agent_id == "claude":
        env.update(agenthome.prepare(settings, "claude", run))
        argv = [*agent.base(), "-p", "표준 입력의 리뷰 요청을 따른다.", "--output-format", "json", "--max-turns", "20",
                "--allowedTools", "Read,Grep,Glob", "--no-session-persistence", *(["--model", model] if model else [])]
        return argv, env, prompt
    if agent_id == "codex":
        env.update(agenthome.prepare(settings, "codex", run))
        argv = [*agent.base(), "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral", "-C", run["target_root"],
                "-o", str(out_file), *(["-m", model] if model else []), "-"]
        return argv, env, prompt
    raise RuntimeError(f"'{agent_id}' 에이전트는 리뷰어로 쓰지 않는다")


def run_review(settings, run: dict, diff: str, files: list[str]) -> dict:
    """리뷰어를 한 번 돌린다 → {"ok", "findings", "error"?, "langs"}."""
    from beom_workbench.core import agenthome
    prompt = build_prompt(diff, files)
    tmp = Path(tempfile.mkdtemp(prefix="wb-review-"))
    out_file = tmp / "last.txt"
    try:
        argv, env, stdin = _argv(settings, run, prompt, out_file)
    except RuntimeError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": False, "error": str(e), "findings": []}
    agent_id = run.get("agent") or "claude"
    try:
        cp = subprocess.run(argv, input=stdin, cwd=run["target_root"], env=env, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=TIMEOUT)
        text = out_file.read_text(encoding="utf-8", errors="replace") if out_file.is_file() else cp.stdout
        cost = 0.0
        if agent_id == "claude":  # 결과는 JSON — 글은 result, 비용은 total_cost_usd (실행 비용에 더한다)
            try:
                j = json.loads(cp.stdout or "{}")
                text, cost = str(j.get("result") or ""), float(j.get("total_cost_usd") or 0)
            except (ValueError, TypeError):
                pass
        if cp.returncode != 0 and not text.strip():
            return {"ok": False, "error": (cp.stderr or "")[-300:] or f"종료 코드 {cp.returncode}", "findings": []}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"{TIMEOUT}초 안에 끝나지 않음", "findings": []}
    except OSError as e:
        return {"ok": False, "error": str(e), "findings": []}
    finally:
        try:
            agenthome.sync_creds(settings, agent_id)
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(tmp, ignore_errors=True)
    return {"ok": True, "findings": parse(text), "langs": languages(files), "cost_usd": round(cost, 4)}
