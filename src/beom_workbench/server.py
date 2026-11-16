"""MCP 서버 인스턴스와 공용 상태(설정·LSP 풀). modules/ 의 도구들이 이 인스턴스에 등록된다."""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from beom_workbench import __version__
from beom_workbench.adapters.lsp import LspPool
from beom_workbench.config import load_settings

settings = load_settings()
lsp_pool = LspPool(settings.lsp_idle_seconds, settings.lsp_timeout_seconds)

mcp = MCPServer(
    "beom-workbench",
    version=__version__,
    instructions=(
        "저장소를 단위별로 전수 분석하고, 요청이나 참고 저장소로 기능을 만들어 검증하는 툴킷입니다. 도구 prefix: wb_*. "
        "흐름: wb_doctor → wb_repo_open → wb_inventory_build → (wb_unit_next → wb_unit_context → "
        "wb_finding_record / wb_symbols_mark / wb_unknown_record → wb_unit_complete) 반복 → wb_report_coverage → wb_report_export. "
        "모든 결론은 근거(파일:줄)가 있어야 하고, 확정 못 한 것은 wb_unknown_record 로 남깁니다. "
        "호출 관계를 결론에 쓰려면 wb_lsp_references / wb_lsp_call_hierarchy 가 돌려준 evidence_id(L1)를 인용하세요. "
        "비밀 파일(secret 범주)은 읽거나 인용하지 않습니다. "
        "포팅(wb_port_*)은 응답의 next_step 을 따르고, 차이 허용·생략·케이스 변경은 제안일 뿐이라 사용자 승인"
        "(wb_port_approve 확인 창 또는 사용자가 터미널에서 port-approve)을 받아야 하며 에이전트가 대신 승인할 수 없습니다. "
        "포팅 완료는 wb_port_finish 가 발급한 판정으로만 보고합니다. 자세한 절차는 beom-workbench 스킬을 따르세요."
    ),
)
