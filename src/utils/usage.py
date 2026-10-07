"""按项目累计 LLM token 用量与费用。

每个项目（论文）持有一个独立的 ``UsageTracker``，多个项目在不同线程中
并发运行时互不干扰；内部用锁保护，便于协调器在其它线程读取快照。
"""
import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _as_price(value: Any) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


class UsageTracker:
    """线程安全的 token 用量累加器。"""

    _TOKEN_FIELDS = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "reasoning_tokens",
        "cached_prompt_tokens",
    )

    def __init__(
        self,
        project: Optional[str] = None,
        model: Optional[str] = None,
        price_input_per_mtok: Any = 0.0,
        price_output_per_mtok: Any = 0.0,
        price_cached_input_per_mtok: Any = None,
    ):
        self.project = project
        self.model = model
        self.price_input_per_mtok = _as_price(price_input_per_mtok)
        self.price_output_per_mtok = _as_price(price_output_per_mtok)
        # 未配置缓存价格时，缓存命中的输入 token 按普通输入价计费。
        self.price_cached_input_per_mtok = (
            None if price_cached_input_per_mtok in (None, "")
            else _as_price(price_cached_input_per_mtok)
        )
        self._lock = threading.Lock()
        self._totals = {field: 0 for field in self._TOKEN_FIELDS}
        self._requests = 0
        self._requests_without_usage = 0
        self._events: Dict[str, int] = {}

    @classmethod
    def from_llm_config(cls, llm_config: Dict[str, Any], project: Optional[str] = None) -> "UsageTracker":
        return cls(
            project=project,
            model=llm_config.get("model"),
            price_input_per_mtok=llm_config.get("price_input_per_mtok", 0.0),
            price_output_per_mtok=llm_config.get("price_output_per_mtok", 0.0),
            price_cached_input_per_mtok=llm_config.get("price_cached_input_per_mtok"),
        )

    @staticmethod
    def parse_usage(usage: Any) -> Optional[Dict[str, int]]:
        """把 OpenAI 兼容响应的 ``usage`` 归一化；缺失时返回 None。"""
        if not isinstance(usage, dict):
            return None
        prompt = _as_int(usage.get("prompt_tokens"))
        completion = _as_int(usage.get("completion_tokens"))
        total = _as_int(usage.get("total_tokens")) or prompt + completion
        completion_details = usage.get("completion_tokens_details") or {}
        prompt_details = usage.get("prompt_tokens_details") or {}
        reasoning = _as_int(
            completion_details.get("reasoning_tokens")
            if isinstance(completion_details, dict) else 0
        )
        cached = _as_int(
            prompt_details.get("cached_tokens")
            if isinstance(prompt_details, dict) else 0
        ) or _as_int(usage.get("prompt_cache_hit_tokens"))  # DeepSeek 字段
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "reasoning_tokens": reasoning,
            "cached_prompt_tokens": min(cached, prompt) if prompt else cached,
        }

    def record(self, usage: Any) -> None:
        """记录一次成功的 HTTP 响应（无论译文最终是否被采用都已计费）。"""
        parsed = self.parse_usage(usage)
        with self._lock:
            self._requests += 1
            if parsed is None:
                self._requests_without_usage += 1
                return
            for field, value in parsed.items():
                self._totals[field] += value

    def record_event(self, name: str, count: int = 1) -> None:
        """记录限流重试等非 token 事件。"""
        with self._lock:
            self._events[name] = self._events.get(name, 0) + count

    def _cost(self, totals: Dict[str, int]) -> Optional[Dict[str, float]]:
        if not self.price_input_per_mtok and not self.price_output_per_mtok:
            return None
        cached = totals["cached_prompt_tokens"]
        uncached = max(0, totals["prompt_tokens"] - cached)
        cached_price = (
            self.price_input_per_mtok
            if self.price_cached_input_per_mtok is None
            else self.price_cached_input_per_mtok
        )
        input_cost = (uncached * self.price_input_per_mtok + cached * cached_price) / 1e6
        output_cost = totals["completion_tokens"] * self.price_output_per_mtok / 1e6
        return {
            "input": round(input_cost, 6),
            "output": round(output_cost, 6),
            "total": round(input_cost + output_cost, 6),
        }

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            totals = dict(self._totals)
            requests = self._requests
            missing = self._requests_without_usage
            events = dict(self._events)
        data: Dict[str, Any] = {
            "project": self.project,
            "model": self.model,
            "requests": requests,
            "requests_without_usage": missing,
            **totals,
        }
        if events:
            data["events"] = events
        cost = self._cost(totals)
        if cost is not None:
            data["cost"] = cost
            data["price_per_mtok"] = {
                "input": self.price_input_per_mtok,
                "output": self.price_output_per_mtok,
                "cached_input": (
                    self.price_input_per_mtok
                    if self.price_cached_input_per_mtok is None
                    else self.price_cached_input_per_mtok
                ),
            }
        return data

    def summary_line(self) -> str:
        data = self.snapshot()
        line = (
            f"LLM 用量：{data['requests']} 次请求，输入 {data['prompt_tokens']} tokens"
            f"（缓存命中 {data['cached_prompt_tokens']}），输出 {data['completion_tokens']} tokens"
        )
        if data["reasoning_tokens"]:
            line += f"（推理 {data['reasoning_tokens']}）"
        line += f"，合计 {data['total_tokens']} tokens"
        if "cost" in data:
            line += f"，估算费用 {data['cost']['total']:.4f}"
        events = data.get("events") or {}
        if events:
            line += "，" + "，".join(f"{name}={count}" for name, count in sorted(events.items()))
        return line

    def write_json(self, path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.snapshot(), f, indent=4, ensure_ascii=False)
        return path
