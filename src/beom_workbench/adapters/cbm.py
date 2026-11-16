"""codebase-memory-mcp 어댑터 — CLI 서브프로세스로만 호출한다 (내부 SQLite 직접 조회 금지).

공식 문서상 check_index_coverage 는 완전성 증명이 아니므로, 커버리지 판정은 원장이 직접 한다.
여기서는 구조 지도(심볼·호출 간선·라우트)와 파싱 품질 신호만 가져온다.
"""
from __future__ import annotations

import json
import re
import os
import subprocess
from pathlib import Path
from typing import Any

from beom_workbench.common.response import ToolError
from beom_workbench.config import Settings

# 엔진이 폴더 권한 때문에 거부할 때 내는 문구 (엔진 0.12 에서 직접 확인함)
_UNTRUSTED = re.compile(r"untrusted identity|could not load saved runtime settings|secure CLI coordination", re.I)

# 분석 단위로 커버리지를 요구하는 심볼 종류
SYMBOL_LABELS = ["Function", "Method", "Class", "Struct", "Interface", "Enum", "Type", "Trait", "Macro", "Impl"]


class CodebaseMemory:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def available(self) -> bool:
        return bool(self.settings.cbm_bin) and Path(self.settings.cbm_bin).exists()

    def _run(self, tool: str, args: dict[str, Any], timeout: int = 1800) -> Any:
        if not self.available:
            raise ToolError(
                "capability_unavailable", "codebase-memory-mcp 가 없음",
                recovery="wb_doctor 안내대로 설치하거나 BEOM_WORKBENCH_CBM 환경변수로 경로 지정",
            )
        env = {**os.environ, "CBM_CACHE_DIR": str(self.settings.cbm_cache)}
        if not env.get("CBM_RUNTIME_DIR"):
            # 엔진은 조정용 폴더를 기본으로 AppData 아래에 만들고, 그 상위 폴더 권한에 다른 계정의 쓰기 권한이 있으면
            # ("untrusted identity") 거부한다 — 툴킷 홈 아래 전용 폴더를 쓴다 (사용자 권한 설정은 바꾸지 않는다)
            run_dir = self.settings.home / "cbm-run"
            run_dir.mkdir(parents=True, exist_ok=True)
            env["CBM_RUNTIME_DIR"] = str(run_dir)
        proc = subprocess.run(
            [self.settings.cbm_bin, "cli", "--quiet", tool],
            input=json.dumps(args).encode("utf-8"), capture_output=True, timeout=timeout, env=env,
        )
        stdout = proc.stdout.decode("utf-8", "replace").strip()
        err = proc.stderr.decode("utf-8", "replace")
        if proc.returncode != 0 or (not stdout and _UNTRUSTED.search(err)):
            # 엔진은 데이터·조정 폴더의 상위 폴더에 다른 계정이 쓸 수 있으면 실행을 거부한다 (보통은 사용자 폴더 아래라 괜찮고,
            # 툴킷 홈을 AppData 같은 곳으로 옮겼을 때 생긴다). 엔진 문구만으로는 원인을 알기 어려워 할 일을 함께 적는다.
            hint = None
            if _UNTRUSTED.search(err):
                hint = (f"분석 엔진이 이 데이터 폴더({self.settings.home})를 쓰지 못합니다 — 상위 폴더에 다른 계정의 쓰기 권한이 있습니다. "
                        "BEOM_WORKBENCH_HOME 을 사용자 폴더 아래(기본값 ~/.beom-workbench)로 두세요")
            raise ToolError("engine_error", f"codebase-memory-mcp {tool} 실패: {err.strip()[-800:]}", recovery=hint)
        # 경고 로그가 stdout 앞에 섞여 나올 수 있어 마지막 JSON 줄을 찾는다
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{") or line.startswith("["):
                return json.loads(line)
        raise ToolError("engine_error", f"codebase-memory-mcp {tool} 응답이 JSON 이 아님: {stdout[-400:]}")

    def version(self) -> str | None:
        if not self.available:
            return None
        proc = subprocess.run([self.settings.cbm_bin, "--version"], capture_output=True, timeout=60)
        return proc.stdout.decode().strip() or None

    def index(self, repo_dir: Path, project: str, exclude: list[str] | None = None) -> dict:
        """인덱싱 후 index_status(diagnostics=full)로 잘리지 않은 파싱 품질 목록을 받아 돌려준다.

        exclude: 구조 지도에 넣지 않을 파일(비밀 파일). 분석 전용 clone 에 .cbmignore 로 기록한다.
        """
        if exclude:
            (repo_dir / ".cbmignore").write_text("\n".join(f"/{p}" for p in exclude) + "\n", encoding="utf-8")
        self._run("index_repository", {"repo_path": str(repo_dir), "name": project, "mode": "full"})
        return self._run("index_status", {"project": project, "diagnostics": "full", "format": "json"})

    def all_calls(self, project: str) -> list[dict]:
        """CALLS 간선 전체 — 인벤토리 때 한 번 받아 원장에 저장한다 (단위마다 CLI 를 부르면 느림)."""
        rows = self.query(
            project,
            "MATCH (a)-[r:CALLS]->(b) RETURN a.qualified_name, a.file_path, a.start_line, b.qualified_name, b.file_path, b.start_line, r",
            page_rows=99998,
        )
        return [self._edge(row) for row in rows]

    @staticmethod
    def _edge(row: list[Any]) -> dict:
        a_qn, a_path, a_line, b_qn, b_path, b_line, props = row
        meta = json.loads(props) if isinstance(props, str) and props.startswith("{") else {}
        return {
            "caller": a_qn, "caller_path": a_path, "caller_line": int(a_line or 0),
            "callee": b_qn, "callee_path": b_path, "callee_line": int(b_line or 0),
            "call_line": meta.get("line"), "confidence": meta.get("confidence"), "strategy": meta.get("strategy"),
        }

    def query(self, project: str, cypher: str, page_rows: int = 5000) -> list[list[Any]]:
        """Cypher 결과 전체를 cursor 로 끝까지 읽는다."""
        rows: list[list[Any]] = []
        cursor: str | None = None
        while True:
            # CLI 호출마다 프로세스 기동 비용(수 초)이 커서, 출력 예산을 넉넉히 줘 한 번에 받는다
            args: dict[str, Any] = {"project": project, "query": cypher, "format": "json", "max_rows": page_rows,
                                    "max_output_tokens": 4_000_000}
            if cursor:
                args["cursor"] = cursor
            page = self._run("query_graph", args)
            rows.extend(page.get("rows", []))
            cursor = page.get("next_cursor") or page.get("cursor")
            if not page.get("has_more") or not cursor:
                if page.get("has_more") and not cursor:
                    raise ToolError("engine_error", "query_graph 결과가 잘렸는데 cursor 가 없음 — page_rows 조정 필요")
                return rows

    def symbols(self, project: str) -> list[dict]:
        labels = ", ".join(f"'{label}'" for label in SYMBOL_LABELS)
        rows = self.query(
            project,
            "MATCH (n) WHERE n.label IN [" + labels + "] "
            "RETURN n.qualified_name, n.name, n.label, n.file_path, n.start_line, n.end_line",
        )
        out = []
        for qn, name, label, path, start, end in rows:
            if not path or path.startswith("<"):  # <python-builtins> 같은 가상 노드
                continue
            out.append({
                "qualified_name": qn, "name": name, "label": label, "path": path,
                "start_line": int(start or 0), "end_line": int(end or start or 0),
            })
        return out

    def routes(self, project: str) -> list[dict]:
        rows = self.query(
            project,
            "MATCH (h)-[r:HANDLES]->(n:Route) RETURN n.name, h.qualified_name, h.file_path, h.start_line, h.end_line",
        )
        return [
            {"route": route, "handler": handler, "path": path, "start_line": int(start or 0), "end_line": int(end or start or 0)}
            for route, handler, path, start, end in rows if path
        ]
