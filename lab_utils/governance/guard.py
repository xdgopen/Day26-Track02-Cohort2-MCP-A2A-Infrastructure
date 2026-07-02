"""Bộ kiểm tra governance cho kết nối MCP và A2A."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from lab_utils.governance.audit import AuditLogger
from lab_utils.governance.models import ConnectionType, GovernanceDecision, GovernanceVerdict
from lab_utils.governance.rate_limit import RateLimiter

POLICY_PATH = Path(__file__).resolve().parent / "policy.json"

# Singleton dùng chung giữa MCP server, A2A agent và orchestrator.
_guard_instance: "GovernanceGuard | None" = None


class GovernanceGuard:
    def __init__(
        self,
        policy_path: Path | None = None,
        audit: AuditLogger | None = None,
    ):
        path = policy_path or POLICY_PATH
        self.policy = json.loads(path.read_text(encoding="utf-8"))
        self.audit = audit or AuditLogger()
        limits = self.policy.get("global_limits", {})
        self.rate_limiter = RateLimiter(
            max_calls_per_minute=int(limits.get("rate_limit_per_minute", 30)),
        )
        self.max_tool_calls = int(limits.get("max_tool_calls_per_task", 50))
        self.cost_ceiling = float(limits.get("cost_ceiling_usd", 10.0))
        self._task_tool_counts: dict[str, int] = {}
        self._task_costs: dict[str, float] = {}

    def authorize_mcp_tool(
        self,
        actor_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        trace_id: str | None = None,
        task_id: str = "default",
    ) -> GovernanceDecision:
        decision = self._check_rate_limit(actor_id, ConnectionType.MCP, tool_name)
        if decision.blocked:
            self._log(decision, "mcp_tool_call", str(arguments), trace_id)
            return decision

        decision = self._check_task_limits(actor_id, ConnectionType.MCP, tool_name, task_id)
        if not decision.allowed:
            self._log(decision, "mcp_tool_call", str(arguments), trace_id)
            return decision

        mcp_policy = self.policy["connections"]["mcp"].get("research-tools", {})
        allowed_callers = mcp_policy.get("allowed_callers", [])
        if actor_id not in allowed_callers:
            decision = GovernanceDecision(
                verdict=GovernanceVerdict.DENY,
                reason=f"Actor '{actor_id}' không được phép gọi MCP server research-tools",
                actor_id=actor_id,
                connection_type=ConnectionType.MCP,
                resource=f"mcp:research-tools/{tool_name}",
            )
            self._log(decision, "mcp_tool_call", str(arguments), trace_id)
            return decision

        tool_policy = mcp_policy.get("tools", {}).get(tool_name)
        if not tool_policy or not tool_policy.get("allowed", False):
            decision = GovernanceDecision(
                verdict=GovernanceVerdict.DENY,
                reason=f"MCP tool '{tool_name}' không nằm trong capability matrix",
                actor_id=actor_id,
                connection_type=ConnectionType.MCP,
                resource=f"mcp:research-tools/{tool_name}",
            )
            self._log(decision, "mcp_tool_call", str(arguments), trace_id)
            return decision

        if tool_name == "search_documents":
            query = str(arguments.get("query", ""))
            max_len = int(tool_policy.get("max_query_length", 500))
            if len(query) > max_len:
                decision = GovernanceDecision(
                    verdict=GovernanceVerdict.DENY,
                    reason=f"Truy vấn vượt giới hạn {max_len} ký tự",
                    actor_id=actor_id,
                    connection_type=ConnectionType.MCP,
                    resource=f"mcp:research-tools/{tool_name}",
                )
                self._log(decision, "mcp_tool_call", query, trace_id)
                return decision
            blocked_keywords = [
                str(keyword).lower()
                for keyword in tool_policy.get("blocked_keywords", [])
            ]
            query_lower = query.lower()
            blocked = [keyword for keyword in blocked_keywords if keyword in query_lower]
            if blocked:
                decision = GovernanceDecision(
                    verdict=GovernanceVerdict.DENY,
                    reason=f"Từ khóa bị chặn trong search_documents: {', '.join(blocked)}",
                    actor_id=actor_id,
                    connection_type=ConnectionType.MCP,
                    resource=f"mcp:research-tools/{tool_name}",
                )
                self._log(decision, "mcp_tool_call", query, trace_id)
                return decision

        if tool_name == "sql_query":
            sql = str(arguments.get("sql", ""))
            sql_decision = self._validate_sql(actor_id, sql, tool_policy)
            if not sql_decision.allowed:
                self._log(sql_decision, "mcp_tool_call", sql, trace_id)
                return sql_decision
            pii_decision = self._check_pii(actor_id, sql, ConnectionType.MCP, tool_name)
            if pii_decision.needs_approval:
                self._log(pii_decision, "mcp_tool_call", sql, trace_id)
                return pii_decision

        if tool_name == "summarize_text":
            text = str(arguments.get("text", ""))
            max_chars = int(tool_policy.get("max_input_chars", 10000))
            if len(text) > max_chars:
                decision = GovernanceDecision(
                    verdict=GovernanceVerdict.DENY,
                    reason=f"Văn bản vượt giới hạn {max_chars} ký tự",
                    actor_id=actor_id,
                    connection_type=ConnectionType.MCP,
                    resource=f"mcp:research-tools/{tool_name}",
                )
                self._log(decision, "mcp_tool_call", text[:200], trace_id)
                return decision

        if tool_name == "count_words":
            text = str(arguments.get("text", ""))
            max_chars = int(tool_policy.get("max_input_chars", 10000))
            if len(text) > max_chars:
                decision = GovernanceDecision(
                    verdict=GovernanceVerdict.DENY,
                    reason=f"Văn bản vượt giới hạn {max_chars} ký tự",
                    actor_id=actor_id,
                    connection_type=ConnectionType.MCP,
                    resource=f"mcp:research-tools/{tool_name}",
                )
                self._log(decision, "mcp_tool_call", text[:200], trace_id)
                return decision

        decision = GovernanceDecision(
            verdict=GovernanceVerdict.ALLOW,
            reason="MCP tool call được phép theo policy",
            actor_id=actor_id,
            connection_type=ConnectionType.MCP,
            resource=f"mcp:research-tools/{tool_name}",
            metadata={"classification": tool_policy.get("data_classification", "internal")},
        )
        self._increment_task_counter(task_id)
        self._log(decision, "mcp_tool_call", str(arguments), trace_id)
        return decision

    def authorize_a2a_dispatch(
        self,
        source_agent: str,
        target_agent: str,
        task_summary: str = "",
        trace_id: str | None = None,
        task_id: str = "default",
    ) -> GovernanceDecision:
        decision = self._check_rate_limit(source_agent, ConnectionType.A2A, target_agent)
        if decision.blocked:
            self._log(decision, "a2a_dispatch", task_summary, trace_id)
            return decision

        decision = self._check_task_limits(source_agent, ConnectionType.A2A, target_agent, task_id)
        if not decision.allowed:
            self._log(decision, "a2a_dispatch", task_summary, trace_id)
            return decision

        a2a_policy = self.policy["connections"]["a2a"]
        source_policy = a2a_policy.get(source_agent, {})
        allowed_targets = source_policy.get("allowed_targets", [])
        if target_agent not in allowed_targets:
            decision = GovernanceDecision(
                verdict=GovernanceVerdict.DENY,
                reason=(
                    f"'{source_agent}' không được dispatch A2A tới '{target_agent}'. "
                    f"Chỉ cho phép: {allowed_targets}"
                ),
                actor_id=source_agent,
                connection_type=ConnectionType.A2A,
                resource=f"a2a:{source_agent}->{target_agent}",
            )
            self._log(decision, "a2a_dispatch", task_summary, trace_id)
            return decision

        target_policy = a2a_policy.get(target_agent, {})
        allowed_callers = target_policy.get("allowed_callers", [])
        if source_agent not in allowed_callers:
            decision = GovernanceDecision(
                verdict=GovernanceVerdict.DENY,
                reason=f"'{target_agent}' không chấp nhận caller '{source_agent}'",
                actor_id=source_agent,
                connection_type=ConnectionType.A2A,
                resource=f"a2a:{source_agent}->{target_agent}",
            )
            self._log(decision, "a2a_dispatch", task_summary, trace_id)
            return decision

        if source_policy.get("require_trace_id") and not trace_id:
            decision = GovernanceDecision(
                verdict=GovernanceVerdict.HITL_REQUIRED,
                reason="A2A dispatch yêu cầu trace_id trong metadata (W3C Trace Context)",
                actor_id=source_agent,
                connection_type=ConnectionType.A2A,
                resource=f"a2a:{source_agent}->{target_agent}",
            )
            self._log(decision, "a2a_dispatch", task_summary, trace_id)
            return decision

        decision = GovernanceDecision(
            verdict=GovernanceVerdict.ALLOW,
            reason="A2A dispatch được phép theo capability matrix",
            actor_id=source_agent,
            connection_type=ConnectionType.A2A,
            resource=f"a2a:{source_agent}->{target_agent}",
        )
        self._increment_task_counter(task_id)
        self._log(decision, "a2a_dispatch", task_summary, trace_id)
        return decision

    def authorize_agent_tool(
        self,
        actor_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        trace_id: str | None = None,
        task_id: str = "default",
    ) -> GovernanceDecision:
        """Kiểm tra tool trên A2A specialist agent (search_web, run_sql_query, ...)."""
        decision = self._check_rate_limit(actor_id, ConnectionType.A2A, tool_name)
        if decision.blocked:
            self._log(decision, "a2a_tool_call", str(arguments), trace_id)
            return decision

        a2a_policy = self.policy["connections"]["a2a"].get(actor_id, {})
        allowed_tools = a2a_policy.get("allowed_tools", [])
        if tool_name not in allowed_tools:
            decision = GovernanceDecision(
                verdict=GovernanceVerdict.DENY,
                reason=f"Agent '{actor_id}' không được gọi tool '{tool_name}'",
                actor_id=actor_id,
                connection_type=ConnectionType.A2A,
                resource=f"a2a-tool:{actor_id}/{tool_name}",
            )
            self._log(decision, "a2a_tool_call", str(arguments), trace_id)
            return decision

        if tool_name == "run_sql_query":
            sql_decision = self._validate_sql(
                actor_id,
                str(arguments.get("sql", "")),
                {"read_only": True, "allowed_tables": ["agent_metrics"]},
            )
            if not sql_decision.allowed:
                self._log(sql_decision, "a2a_tool_call", str(arguments), trace_id)
                return sql_decision

        decision = GovernanceDecision(
            verdict=GovernanceVerdict.ALLOW,
            reason="A2A tool call được phép",
            actor_id=actor_id,
            connection_type=ConnectionType.A2A,
            resource=f"a2a-tool:{actor_id}/{tool_name}",
        )
        self._increment_task_counter(task_id)
        self._log(decision, "a2a_tool_call", str(arguments), trace_id)
        return decision

    def authorize_mcp_connection(self, caller_id: str) -> GovernanceDecision:
        """Kiểm tra khi thiết lập kết nối MCP (trước khi spawn subprocess)."""
        mcp_policy = self.policy["connections"]["mcp"].get("research-tools", {})
        allowed_callers = mcp_policy.get("allowed_callers", [])
        if caller_id not in allowed_callers:
            return GovernanceDecision(
                verdict=GovernanceVerdict.DENY,
                reason=f"'{caller_id}' không được phép mở kết nối MCP research-tools",
                actor_id=caller_id,
                connection_type=ConnectionType.MCP,
                resource="mcp:research-tools",
            )
        allowed_tools = [
            name
            for name, cfg in mcp_policy.get("tools", {}).items()
            if cfg.get("allowed")
        ]
        return GovernanceDecision(
            verdict=GovernanceVerdict.ALLOW,
            reason="Kết nối MCP được phép",
            actor_id=caller_id,
            connection_type=ConnectionType.MCP,
            resource="mcp:research-tools",
            metadata={"allowed_tools": allowed_tools},
        )

    def record_cost(self, task_id: str, amount_usd: float, actor_id: str) -> GovernanceDecision:
        total = self._task_costs.get(task_id, 0.0) + amount_usd
        self._task_costs[task_id] = total
        if total > self.cost_ceiling:
            decision = GovernanceDecision(
                verdict=GovernanceVerdict.HITL_REQUIRED,
                reason=f"Chi phí task ${total:.2f} vượt trần ${self.cost_ceiling:.2f} — cần phê duyệt",
                actor_id=actor_id,
                connection_type=ConnectionType.A2A,
                resource="cost:ceiling",
                metadata={"total_cost_usd": total},
            )
            self._log(decision, "cost_check", f"+${amount_usd:.4f}", None)
            return decision
        return GovernanceDecision(
            verdict=GovernanceVerdict.ALLOW,
            reason="Chi phí trong giới hạn",
            actor_id=actor_id,
            connection_type=ConnectionType.A2A,
            resource="cost:ceiling",
            metadata={"total_cost_usd": total},
        )

    def get_allowed_mcp_tools(self, caller_id: str) -> list[str]:
        conn = self.authorize_mcp_connection(caller_id)
        if not conn.allowed:
            return []
        return conn.metadata.get("allowed_tools", [])

    def _validate_sql(
        self,
        actor_id: str,
        sql: str,
        tool_policy: dict[str, Any],
    ) -> GovernanceDecision:
        sql_upper = sql.strip().upper()
        resource = "sql:validation"

        if not sql_upper.startswith("SELECT"):
            return GovernanceDecision(
                verdict=GovernanceVerdict.DENY,
                reason="Chỉ cho phép SELECT (read-only)",
                actor_id=actor_id,
                connection_type=ConnectionType.MCP,
                resource=resource,
            )

        forbidden = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE")
        if any(token in sql_upper for token in forbidden):
            return GovernanceDecision(
                verdict=GovernanceVerdict.DENY,
                reason="Câu lệnh SQL ghi/DDL bị chặn bởi governance",
                actor_id=actor_id,
                connection_type=ConnectionType.MCP,
                resource=resource,
            )

        allowed_tables = [t.upper() for t in tool_policy.get("allowed_tables", [])]
        if allowed_tables and not any(table in sql_upper for table in allowed_tables):
            return GovernanceDecision(
                verdict=GovernanceVerdict.DENY,
                reason=f"Chỉ được truy vấn bảng: {allowed_tables}",
                actor_id=actor_id,
                connection_type=ConnectionType.MCP,
                resource=resource,
            )

        return GovernanceDecision(
            verdict=GovernanceVerdict.ALLOW,
            reason="SQL read-only hợp lệ",
            actor_id=actor_id,
            connection_type=ConnectionType.MCP,
            resource=resource,
        )

    def _check_pii(
        self,
        actor_id: str,
        content: str,
        connection_type: ConnectionType,
        resource: str,
    ) -> GovernanceDecision:
        patterns = self.policy.get("pii_patterns", [])
        for pattern in patterns:
            if re.search(pattern, content):
                return GovernanceDecision(
                    verdict=GovernanceVerdict.HITL_REQUIRED,
                    reason="Phát hiện dữ liệu nhạy cảm (PII) — cần phê duyệt HITL",
                    actor_id=actor_id,
                    connection_type=connection_type,
                    resource=resource,
                )
        return GovernanceDecision(
            verdict=GovernanceVerdict.ALLOW,
            reason="Không phát hiện PII",
            actor_id=actor_id,
            connection_type=connection_type,
            resource=resource,
        )

    def _check_rate_limit(
        self,
        actor_id: str,
        connection_type: ConnectionType,
        resource: str,
    ) -> GovernanceDecision:
        ok, message = self.rate_limiter.check(actor_id)
        if not ok:
            return GovernanceDecision(
                verdict=GovernanceVerdict.DENY,
                reason=message,
                actor_id=actor_id,
                connection_type=connection_type,
                resource=resource,
            )
        return GovernanceDecision(
            verdict=GovernanceVerdict.ALLOW,
            reason="Rate limit OK",
            actor_id=actor_id,
            connection_type=connection_type,
            resource=resource,
        )

    def _check_task_limits(
        self,
        actor_id: str,
        connection_type: ConnectionType,
        resource: str,
        task_id: str,
    ) -> GovernanceDecision:
        count = self._task_tool_counts.get(task_id, 0)
        if count >= self.max_tool_calls:
            return GovernanceDecision(
                verdict=GovernanceVerdict.DENY,
                reason=f"Vượt giới hạn {self.max_tool_calls} tool calls/task (chống chạy vô hạn)",
                actor_id=actor_id,
                connection_type=connection_type,
                resource=resource,
            )
        return GovernanceDecision(
            verdict=GovernanceVerdict.ALLOW,
            reason="Task limits OK",
            actor_id=actor_id,
            connection_type=connection_type,
            resource=resource,
        )

    def _increment_task_counter(self, task_id: str) -> None:
        self._task_tool_counts[task_id] = self._task_tool_counts.get(task_id, 0) + 1

    def _log(
        self,
        decision: GovernanceDecision,
        action: str,
        input_summary: str,
        trace_id: str | None,
    ) -> None:
        self.audit.record(decision, action, input_summary, trace_id)


def get_guard() -> GovernanceGuard:
    global _guard_instance
    if _guard_instance is None:
        _guard_instance = GovernanceGuard()
    return _guard_instance
