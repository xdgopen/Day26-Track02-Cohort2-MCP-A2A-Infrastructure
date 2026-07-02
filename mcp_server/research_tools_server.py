"""Ngày 26 — MCP server cung cấp research tools (transport stdio).

Sinh viên chạy file này như subprocess; ADK kết nối qua McpToolset.
Mọi lần gọi tool đều đi qua GovernanceGuard (audit + policy).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lab_utils.governance import get_guard

GOVERNANCE_ACTOR_ID = os.getenv("GOVERNANCE_ACTOR_ID", "orchestrator")
GOVERNANCE_TRACE_ID = os.getenv("GOVERNANCE_TRACE_ID")
GOVERNANCE_TASK_ID = os.getenv("GOVERNANCE_TASK_ID", "default")

# Kho tài liệu mô phỏng cho demo lớp học (không cần API bên ngoài).
DOCUMENTS: list[dict[str, str]] = [
    {
        "id": "doc-1",
        "title": "VinUni AI Curriculum Overview",
        "body": "Phase 2 Track 2 covers MCP, A2A, and multi-agent orchestration.",
    },
    {
        "id": "doc-2",
        "title": "MCP Transport Options",
        "body": "MCP supports stdio for local dev and HTTP+SSE for remote deployment.",
    },
    {
        "id": "doc-3",
        "title": "A2A Task Lifecycle",
        "body": "Tasks move through submitted, working, input-required, completed, or failed.",
    },
]

SQL_ROWS: list[dict[str, Any]] = [
    {"agent": "search_agent", "tasks_completed": 42, "avg_latency_ms": 820},
    {"agent": "database_agent", "tasks_completed": 31, "avg_latency_ms": 1100},
    {"agent": "synthesis_agent", "tasks_completed": 28, "avg_latency_ms": 2400},
]

app = Server("research-tools")
guard = get_guard()


@app.list_tools()
async def list_tools() -> list[Tool]:
    allowed = set(guard.get_allowed_mcp_tools(GOVERNANCE_ACTOR_ID))
    all_tools = [
        Tool(
            name="search_documents",
            description="Tìm kiếm trong chỉ mục tài liệu nghiên cứu theo từ khóa.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Từ khóa tìm kiếm"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="sql_query",
            description="Thực thi truy vấn SQL chỉ đọc trên metrics agent.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "Câu SQL chỉ SELECT"},
                },
                "required": ["sql"],
            },
        ),
        Tool(
            name="summarize_text",
            description="Tóm tắt đoạn văn thành các gạch đầu dòng.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Văn bản cần tóm tắt"},
                    "max_bullets": {
                        "type": "integer",
                        "description": "Số gạch đầu dòng tối đa",
                        "default": 3,
                    },
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="count_words",
            description="Đếm số từ trong một chuỗi văn bản.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Văn bản cần đếm từ"},
                },
                "required": ["text"],
            },
        ),
    ]
    return [tool for tool in all_tools if tool.name in allowed]


def _search_documents(query: str) -> list[dict[str, str]]:
    query_lower = query.lower()
    return [
        doc
        for doc in DOCUMENTS
        if query_lower in doc["title"].lower() or query_lower in doc["body"].lower()
    ]


def _sql_query(sql: str) -> list[dict[str, Any]]:
    if "AGENT_METRICS" not in sql.upper():
        return []
    return SQL_ROWS


def _summarize_text(text: str, max_bullets: int = 3) -> list[str]:
    sentences = [s.strip() for s in text.replace("\n", " ").split(".") if s.strip()]
    return [f"- {sentence}" for sentence in sentences[:max_bullets]]


def _count_words(text: str) -> int:
    return len(text.split())


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    decision = guard.authorize_mcp_tool(
        actor_id=GOVERNANCE_ACTOR_ID,
        tool_name=name,
        arguments=arguments,
        trace_id=GOVERNANCE_TRACE_ID,
        task_id=GOVERNANCE_TASK_ID,
    )
    if decision.blocked:
        return [TextContent(
            type="text",
            text=json.dumps({
                "status": "blocked",
                "governance": decision.verdict.value,
                "reason": decision.reason,
            }, ensure_ascii=False),
        )]
    if decision.needs_approval:
        return [TextContent(
            type="text",
            text=json.dumps({
                "status": "hitl_required",
                "governance": decision.verdict.value,
                "reason": decision.reason,
                "message": "Cần phê duyệt của người trước khi thực thi.",
            }, ensure_ascii=False),
        )]

    if name == "search_documents":
        results = _search_documents(arguments["query"])
        return [TextContent(type="text", text=json.dumps(results, indent=2, ensure_ascii=False))]
    if name == "sql_query":
        rows = _sql_query(arguments["sql"])
        return [TextContent(type="text", text=json.dumps(rows, indent=2, ensure_ascii=False))]
    if name == "summarize_text":
        bullets = _summarize_text(
            arguments["text"],
            int(arguments.get("max_bullets", 3)),
        )
        return [TextContent(type="text", text="\n".join(bullets))]
    if name == "count_words":
        count = _count_words(arguments["text"])
        return [TextContent(type="text", text=json.dumps({"word_count": count}, ensure_ascii=False))]
    raise ValueError(f"Tool không xác định: {name}")


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
