# Báo cáo Lab #26 - Hạ tầng MCP/A2A & Agentic Routing

**Sinh viên:** Nguyễn Danh Thành  
**MSSV:** 2A202600581  
**Chủ đề:** MCP, A2A, semantic routing, data governance và audit trace

## 1. Tổng quan kết quả

| Hạng mục | Trạng thái | Minh chứng |
|---|---|---|
| MCP server có `search_documents`, `sql_query`, `summarize_text`, `count_words` | Hoàn thành | `mcp_server/research_tools_server.py`, `lab_utils/governance/policy.json` |
| A2A specialists `search_agent`, `database_agent`, `synthesis_agent` | Hoàn thành | Agent cards/ADK Web trace |
| Orchestrator kết nối 3 specialist qua A2A | Hoàn thành | `agents/orchestrator/agent.py`, screenshots ADK Web |
| Semantic router và tool `suggest_routing` | Hoàn thành | `lab_utils/semantic_router.py`, `lab_utils/routing_tool.py`, `screenshots/3.png` |
| Fallback chain `route_with_chain()` | Hoàn thành | `lab_utils/semantic_router.py` |
| Governance policy và audit log | Hoàn thành một phần minh chứng | `lab_utils/governance/policy.json`, `logs/governance_audit.jsonl`, `screenshots/governance_audit.png` |

## 2. Bài tập 1.1 - Khám phá MCP Server

Ba tool MCP ban đầu:

- `search_documents`: tìm kiếm trong kho tài liệu nghiên cứu mô phỏng.
- `sql_query`: chạy truy vấn SQL chỉ đọc trên bảng metrics agent.
- `summarize_text`: tóm tắt văn bản thành các gạch đầu dòng.

Governance cho SQL được enforce qua `GovernanceGuard.authorize_mcp_tool()` và `_validate_sql()`:

- Chỉ cho phép câu lệnh bắt đầu bằng `SELECT`.
- Chặn DDL/DML như `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `TRUNCATE`.
- Chỉ cho phép truy vấn bảng trong allowlist, hiện là `agent_metrics`.
- SQL chứa PII được đưa về trạng thái `hitl_required`.

Transport `stdio` phù hợp khi phát triển local vì ADK có thể spawn MCP server như subprocess, không cần mở public port, giảm cấu hình mạng và phù hợp môi trường lab.

## 3. Bài tập 1.2 - MCP Tool `count_words`

Đã bổ sung tool thứ tư `count_words`:

- Thêm schema trong `list_tools()`.
- Thêm handler trong `call_tool()`.
- Thêm capability trong `policy.json`.
- Cập nhật orchestrator instruction/tool filter theo policy để nhận biết tool mới.

## 4. Bài tập 2.1 - A2A vs Sub-Agent Local

| Tiêu chí | A2A Remote | Sub-Agent Local |
|---|---|---|
| Triển khai | Agent chạy như service riêng, có agent card và endpoint riêng | Agent nằm cùng process với orchestrator |
| Hiệu năng | Có overhead network nhưng scale độc lập | Latency thấp hơn do gọi nội bộ |
| Cô lập state | Cô lập tốt theo service, dễ monitor và deploy riêng | Dễ chia sẻ state nhưng coupling cao hơn |
| Phù hợp khi | Nhiều team/runtime, cần scale, governance, observability riêng | Prototype, workflow nhỏ, yêu cầu đơn giản |

Chọn A2A khi specialist cần lifecycle độc lập, boundary bảo mật rõ, health check, audit, hoặc scale riêng theo tải.

## 5. Bài tập 3.1 - Fallback Chain

Đã thêm `route_with_chain()` trong `SemanticRouter`:

```python
def route_with_chain(self, request: str, chain: list[str]) -> str:
    """Thử route chính; nếu điểm thấp, chọn fallback đầu tiên trong chuỗi."""
```

Kết quả kỳ vọng:

| Input | Chain | Kết quả |
|---|---|---|
| `SELECT latency from agent_metrics` | `["search_agent", "database_agent", "orchestrator"]` | `database_agent` |
| `zzzz yyyy` | `["search_agent", "database_agent", "orchestrator"]` | `search_agent` |

## 6. Bài tập 5.1 - Ma trận Governance

| Agent | Tool được gọi | Cần phê duyệt người | Rate limit | Giới hạn task |
|---|---|---|---|---|
| `orchestrator` | MCP: `search_documents`, `sql_query`, `summarize_text`, `count_words`; A2A tới `search_agent`, `database_agent`, `synthesis_agent`; `suggest_routing` | SQL có PII, vượt cost ceiling, dispatch ngoài allowlist | 30 calls/phút | 50 tool calls/task, 300 giây/task |
| `search_agent` | `search_web` | Truy vấn chứa keyword bị chặn hoặc hành động ghi/xóa/gửi email | 30 calls/phút | 50 tool calls/task |
| `database_agent` | `run_sql_query` | SQL có PII hoặc truy vấn không hợp lệ | 30 calls/phút | Chỉ `SELECT`, chỉ bảng `agent_metrics` |
| `synthesis_agent` | `synthesize_report` | Xuất dữ liệu nhạy cảm hoặc hành động ghi/xóa/gửi email | 30 calls/phút | 50 tool calls/task |

## 7. Bài tập 5.2 - Mở rộng Governance

Đã thực hiện:

- `orchestrator.allowed_targets` có `synthesis_agent`.
- `search_documents` có rule chặn từ khóa `password`.
- `GovernanceGuard` đọc `blocked_keywords` và trả verdict `deny`.
- `count_words` được thêm vào capability matrix.

Audit log hiện có minh chứng các event A2A `allow`, gồm:

- `orchestrator -> search_agent`
- `orchestrator -> database_agent`
- `orchestrator -> synthesis_agent`
- `search_agent/search_web`
- `synthesis_agent/synthesize_report`

Lưu ý: file `logs/governance_audit.jsonl` hiện có 9 entries và đều là `allow`; chưa thấy event `deny` hoặc `hitl_required` trong audit JSONL được cung cấp.

## 8. Kết quả ADK Web

| Mã | Mục tiêu | Kết quả quan sát | Minh chứng |
|---|---|---|---|
| W1 | Transfer sang `search_agent` để tìm multi-agent orchestration | Đạt: trace có `transfer_to_agent` và `search_agent` | `screenshots/1.png`, audit `a2a:orchestrator->search_agent`, `search_web` |
| W2 | Dùng MCP `search_documents`, `sql_query`, `summarize_text` | Chưa đủ minh chứng trong audit JSONL hiện tại; screenshot có prompt nhưng chưa thấy rõ MCP calls | `screenshots/1.png` |
| W3 | Transfer sang `synthesis_agent` để tổng hợp executive report | Đạt: trace có `transfer_to_agent` và `synthesis_agent` | `screenshots/2.png`, audit `a2a:orchestrator->synthesis_agent`, `synthesize_report` |
| W4 | Gọi `suggest_routing` cho truy vấn SQL/metrics | Đạt: trace có tool `suggest_routing` | `screenshots/3.png` |
| W5 | Thử `DROP TABLE agent_metrics` | Không thấy tool SQL/DDL được thực thi trong trace; chưa có audit `deny` trong JSONL | `screenshots/3.png` |

## 9. Minh chứng đính kèm

- `screenshots/1.png`: ADK Web trace cho W1 và prompt W2.
- `screenshots/2.png`: ADK Web trace cho W3, transfer sang `synthesis_agent`.
- `screenshots/3.png`: ADK Web trace cho W4 với `suggest_routing` và W5 với prompt `DROP TABLE`.
- `screenshots/governance_audit.png`: ảnh audit log các event A2A/tool call.
- `logs/governance_audit.jsonl`: audit log dạng JSONL.

## 10. Kết luận

Lab đã hoàn thiện phần triển khai chính: MCP tools, A2A specialists, orchestrator, semantic routing, fallback chain và governance policy. Demo ADK Web đã có minh chứng rõ cho A2A routing sang `search_agent`, `synthesis_agent` và tool `suggest_routing`. Audit log đã ghi các event A2A/tool call `allow`; để hoàn chỉnh tuyệt đối phần governance negative test, cần bổ sung thêm audit event `deny` hoặc `hitl_required` cho tình huống bị chặn.
