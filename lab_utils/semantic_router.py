"""Semantic router không cần embedding — dùng cho demo lớp học.

Dùng cosine similarity bag-of-words để sinh viên chạy không cần API key thêm.
Trong phần mở rộng capstone, thay bằng model embedding thật.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass


def _tokenize(text: str) -> dict[str, float]:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    counts: dict[str, float] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0.0) + 1.0
    return counts


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(a.get(k, 0.0) * b.get(k, 0.0) for k in set(a) | set(b))
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


@dataclass
class AgentCapability:
    name: str
    description: str
    tags: list[str]


class SemanticRouter:
    """Định tuyến yêu cầu người dùng tới specialist agent phù hợp nhất."""

    def __init__(self, agents: list[AgentCapability], threshold: float = 0.15):
        self.agents = agents
        self.threshold = threshold

    def route(self, request: str, top_k: int = 1) -> list[tuple[str, float]]:
        request_vec = _tokenize(request)
        scored: list[tuple[str, float]] = []
        for agent in self.agents:
            corpus = " ".join([agent.description, " ".join(agent.tags)])
            score = _cosine(request_vec, _tokenize(corpus))
            scored.append((agent.name, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_k]

    def route_with_fallback(
        self,
        request: str,
        fallback: str = "orchestrator",
    ) -> str:
        candidates = self.route(request, top_k=1)
        if not candidates:
            return fallback
        name, score = candidates[0]
        return name if score >= self.threshold else fallback

    def route_with_chain(self, request: str, chain: list[str]) -> str:
        """Thử route chính; nếu điểm thấp, chọn fallback đầu tiên trong chuỗi."""
        candidates = self.route(request, top_k=1)
        if candidates:
            name, score = candidates[0]
            if score >= self.threshold:
                return name
        return chain[0] if chain else "orchestrator"
