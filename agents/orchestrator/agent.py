"""Orchestrator — tiêu thụ specialist A2A remote và MCP tools (có governance)."""

import os
import sys
import uuid
from pathlib import Path

from google.adk.agents import Agent
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.tools.mcp_tool import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.genai import types
from mcp import StdioServerParameters

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lab_utils.env_setup import load_lab_env, require_api_key

load_lab_env()
require_api_key()

from lab_utils.governance import (
    get_guard,
    governance_before_agent_callback,
    governance_before_tool_callback,
)
from lab_utils.routing_tool import suggest_routing

SEARCH_CARD = os.getenv(
    "SEARCH_AGENT_CARD",
    "http://localhost:8001/.well-known/agent-card.json",
)
DATABASE_CARD = os.getenv(
    "DATABASE_AGENT_CARD",
    "http://localhost:8002/.well-known/agent-card.json",
)
SYNTHESIS_CARD = os.getenv(
    "SYNTHESIS_AGENT_CARD",
    "http://localhost:8003/.well-known/agent-card.json",
)

MCP_SERVER = PROJECT_ROOT / "mcp_server" / "research_tools_server.py"
GOVERNANCE_ACTOR = "orchestrator"
guard = get_guard()

# Kiểm tra kết nối MCP trước khi spawn subprocess
_mcp_conn = guard.authorize_mcp_connection(GOVERNANCE_ACTOR)
if not _mcp_conn.allowed:
    raise RuntimeError(f"MCP governance: {_mcp_conn.reason}")

_allowed_mcp_tools = guard.get_allowed_mcp_tools(GOVERNANCE_ACTOR)

search_specialist = RemoteA2aAgent(
    name="search_agent",
    description="Tìm kiếm tài liệu và web để thu thập bằng chứng nghiên cứu.",
    agent_card=SEARCH_CARD,
)

database_specialist = RemoteA2aAgent(
    name="database_agent",
    description="Chạy SQL chỉ đọc trên bảng metrics của agent.",
    agent_card=DATABASE_CARD,
)

synthesis_specialist = RemoteA2aAgent(
    name="synthesis_agent",
    description="Tổng hợp kết quả nghiên cứu thành báo cáo cuối có cấu trúc.",
    agent_card=SYNTHESIS_CARD,
)

# Validate A2A connections theo capability matrix
for target in ("search_agent", "database_agent", "synthesis_agent"):
    decision = guard.authorize_a2a_dispatch(
        source_agent=GOVERNANCE_ACTOR,
        target_agent=target,
        task_summary="startup_validation",
        trace_id=str(uuid.uuid4()),
    )
    if not decision.allowed:
        raise RuntimeError(f"A2A governance: {decision.reason}")

mcp_env = {
    **os.environ,
    "GOVERNANCE_ACTOR_ID": GOVERNANCE_ACTOR,
    "GOVERNANCE_TASK_ID": "orchestrator-session",
}

mcp_tools = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=[str(MCP_SERVER)],
            env=mcp_env,
        ),
        timeout=10,
    ),
    tool_filter=_allowed_mcp_tools,
)

root_agent = Agent(
    name="orchestrator",
    model="gemini-2.5-flash",
    description="Điều phối nghiên cứu bằng cách ủy quyền cho search, database và synthesis specialist.",
    generate_content_config=types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    ),
    instruction="""Bạn là orchestrator nghiên cứu cho hệ multi-agent.

QUAN TRỌNG — luôn làm theo thứ tự:
1. Trả lời người dùng bằng ít nhất một câu văn bản (tiếng Việt).
2. Nếu cần ủy quyền, gọi tool transfer_to_agent(agent_name="...") NGAY trong cùng lượt.

Quy tắc định tuyến:
- Tra cứu web / tài liệu → transfer_to_agent(agent_name="search_agent")
- Metrics / SQL → transfer_to_agent(agent_name="database_agent")
- Tổng hợp báo cáo cuối → transfer_to_agent(agent_name="synthesis_agent")
- MCP local: search_documents, sql_query, summarize_text, count_words (khi không cần A2A)
- suggest_routing: chỉ khi không chắc chọn agent nào

Ví dụ: user nói "Chuyển sang search_agent..." →
  (a) trả lời "Tôi sẽ ủy quyền cho search_agent..."
  (b) gọi transfer_to_agent(agent_name="search_agent")

Data governance:
- Không SQL ghi/DDL.
- Nếu blocked hoặc hitl_required → báo user và dừng.
""",
    tools=[mcp_tools, suggest_routing],
    sub_agents=[search_specialist, database_specialist, synthesis_specialist],
    before_tool_callback=governance_before_tool_callback,
    before_agent_callback=governance_before_agent_callback,
)
