from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from urllib.parse import urlparse
import json
import yaml
import toml
from pathlib import Path


class TruncatedResponseError(ValueError):
    """模型输出因 max_tokens 被截断。"""


class BaseToolAgent(ABC):
    """
    Abstract base class for all tool agents in the multi-agent translation system.

    Each tool agent is responsible for a specific task in the translation workflow,
    such as parsing, translating, refining, or validating documents.
    """

    def __init__(
        self,
        agent_name: str,
        config: Optional[Dict[str, Any]] = None
    ):
        """
        Initializes the BaseToolAgent.

        Args:
            agent_name (str): The unique name of this agent (e.g., "ParserAgent", "TranslatorAgent").
            config (Optional[Dict[str, Any]]): Agent-specific configuration parameters
                                               loaded from the TOML files. Defaults to None.
        """
        self.agent_name = agent_name
        self.config = config if config is not None else {}
        # 子类可设置为 src.utils.usage.UsageTracker，用于累计 token 用量。
        self.usage = None

    def log(self, message: str, level: str = "info"):
        """
        Logs messages at different levels (info, debug, warning, error).

        Args:
            message (str): The message to log.
            level (str): The logging level. Defaults to "info".
        """
        if level == "info":
            print(f"[{self.agent_name}] [INFO] {message}")
        elif level == "debug":
            print(f"[{self.agent_name}] [DEBUG] {message}")
        elif level == "warning":
            print(f"[{self.agent_name}] [WARNING] {message}")
        elif level == "error":
            print(f"[{self.agent_name}] [ERROR] {message}")
        else:
            raise ValueError(f"Unknown log level: {level}")

    @abstractmethod
    def execute(self, data: Any, **kwargs: Any) -> Any:
        """
        Executes the core task of the agent.

        This method must be implemented by all concrete tool agent subclasses.
        The input `data` and the returned `Any` type will vary depending on the
        specific agent's role in the workflow (e.g., file path, text string,
        parsed document object, translation result).
        """
        raise NotImplementedError(f"{self.__class__.__name__}.execute() must be implemented.")

    def get_config(self, key: str, default: Any = None) -> Any:
        """
        Retrieves a configuration value for the agent.
        If the key does not exist, returns the provided default value.
        """
        return self.config.get(key, default)

    def get_llm_config(self) -> Dict[str, Any]:
        """
        读取统一的大模型配置。
        """
        return self.config.get("llm_config", {})

    def get_chat_completions_url(self) -> str:
        """
        将 OpenAI 兼容 base_url 规范化为 chat/completions 请求地址。
        """
        llm_config = self.get_llm_config()
        base_url = str(llm_config.get("base_url", "")).strip()
        if not base_url:
            raise ValueError("llm_config.base_url is required.")

        normalized = base_url.rstrip("/")
        parsed = urlparse(normalized)
        if parsed.path.endswith("/chat/completions"):
            return normalized
        if parsed.path.endswith("/v1"):
            return f"{normalized}/chat/completions"
        return f"{normalized}/chat/completions"

    def get_concurrency_limit(self, default: int = 10) -> int:
        """
        读取并发上限，配置异常时回退到保守默认值。
        """
        llm_config = self.get_llm_config()
        raw_value = llm_config.get("concurrency_limit", default)
        try:
            return max(1, int(raw_value))
        except (TypeError, ValueError):
            return default

    def get_request_timeout(self, default: float = 300.0) -> float:
        """单次请求的读超时（秒）。长章节生成 8192 token 可能超过 100 秒。

        优先读取本地的 ``request_timeout``；兼容上游配置中的 ``timeout``
        （上游含义为请求总时长，这里作为读超时使用）。
        """
        llm_config = self.get_llm_config()
        raw_value = llm_config.get("request_timeout")
        if raw_value in (None, ""):
            raw_value = llm_config.get("timeout", default)
        try:
            return max(10.0, float(raw_value))
        except (TypeError, ValueError):
            return default

    def _get_float_llm_config(self, key: str, default: float, minimum: float = 0.0) -> float:
        try:
            return max(minimum, float(self.get_llm_config().get(key, default)))
        except (TypeError, ValueError):
            return default

    def get_max_tokens(self, default: int = 8192) -> int:
        try:
            return max(1, int(self.get_llm_config().get("max_tokens", default)))
        except (TypeError, ValueError):
            return default

    def get_temperature(self, default: float = 0.7) -> float:
        return self._get_float_llm_config("temperature", default)

    def get_rate_limit_settings(self):
        """429/5xx 退避参数：(最大重试次数, 基础等待秒数, 最大等待秒数)。"""
        try:
            retries = max(0, int(self.get_llm_config().get("rate_limit_retries", 6)))
        except (TypeError, ValueError):
            retries = 6
        base = self._get_float_llm_config("rate_limit_backoff_base", 2.0)
        cap = self._get_float_llm_config("rate_limit_backoff_max", 60.0)
        return retries, base, max(base, cap)

    def record_usage(self, result: Any) -> None:
        """把响应中的 ``usage`` 记入本代理的用量统计（如已配置）。"""
        tracker = getattr(self, "usage", None)
        if tracker is not None and isinstance(result, dict):
            tracker.record(result.get("usage"))

    def _record_usage_event(self, name: str) -> None:
        tracker = getattr(self, "usage", None)
        if tracker is not None:
            tracker.record_event(name)

    async def post_chat_with_backoff(
        self,
        session: Any,
        url: str,
        payload: Dict[str, Any],
        headers: Dict[str, str],
        limiter: Any = None,
        timeout: Any = None,
    ) -> Dict[str, Any]:
        """发送一次 chat/completions 请求。

        HTTP 429 及 502/503/504 在这里按指数退避（遵守 Retry-After）重试，
        不消耗调用方“模型输出失败”的重试次数；429/503 还会让 ``limiter``
        临时降低并发并进入共享冷却期。重试耗尽后抛出 ``ClientResponseError``。
        """
        import asyncio
        from src.utils.rate_limit import (
            RETRYABLE_STATUSES,
            THROTTLE_STATUSES,
            backoff_delay,
            parse_retry_after,
        )

        retries, base, cap = self.get_rate_limit_settings()
        attempt = 0
        while True:
            delay = None
            status = None
            if limiter is not None:
                await limiter.acquire()
            try:
                async with session.post(url, json=payload, headers=headers, timeout=timeout) as response:
                    status = getattr(response, "status", 200)
                    if (
                        isinstance(status, int)
                        and status in RETRYABLE_STATUSES
                        and attempt < retries
                    ):
                        retry_after = parse_retry_after(getattr(response, "headers", None))
                        delay = backoff_delay(attempt, base, cap, retry_after)
                        if status in THROTTLE_STATUSES and limiter is not None:
                            if limiter.on_throttle(delay):
                                self.log(
                                    f"⚠️ 接口限流/过载（HTTP {status}），并发上限降至 "
                                    f"{limiter.limit}，冷却 {delay:.1f} 秒后继续。",
                                    level="warning",
                                )
                    else:
                        response.raise_for_status()
                        result = await response.json()
                        if limiter is not None:
                            limiter.on_success()
                        self.record_usage(result)
                        return result
            finally:
                if limiter is not None:
                    limiter.release()
            self._record_usage_event(
                "rate_limited_retries" if status == 429 else "server_error_retries"
            )
            await asyncio.sleep(delay)
            attempt += 1

    def get_aiohttp_timeout(self):
        """aiohttp 的 timeout=数字 表示总时长；这里改为连接/读取分别限时。"""
        import aiohttp

        return aiohttp.ClientTimeout(
            total=None,
            sock_connect=30,
            sock_read=self.get_request_timeout(),
        )

    def get_requests_timeout(self):
        """requests 使用 (连接超时, 读超时)。"""
        return (30, self.get_request_timeout())

    @staticmethod
    def extract_chat_content(result: Any) -> str:
        """从 OpenAI 兼容响应中取出文本。

        ``content`` 为 null（内容过滤、思考模型）或输出因 max_tokens 被截断
        时抛出 ValueError，交给调用方按可重试失败处理，而不是崩溃或把
        半截译文当作结果。
        """
        choices = result.get("choices") if isinstance(result, dict) else None
        if not choices or not isinstance(choices[0], dict):
            raise ValueError(f"API 响应缺少 choices：{str(result)[:200]}")
        choice = choices[0]
        if choice.get("finish_reason") == "length":
            raise TruncatedResponseError("模型输出达到 max_tokens 上限，结果被截断")
        content = (choice.get("message") or {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("模型返回了空内容")
        return content.strip()

    def build_chat_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        为 OpenAI 兼容接口补齐可选字段。
        """
        llm_config = self.get_llm_config()
        final_payload = dict(payload)
        thinking_type = str(llm_config.get("thinking_type", "")).strip().lower()
        if thinking_type in {"enabled", "disabled"}:
            final_payload["thinking"] = {"type": thinking_type}
        return final_payload
    
    def read_file(self, file_path: str, file_format: str) -> str:
        """
        Reads a file and returns its content.
        """
        if file_format == "json":
            with open(file_path, "r", encoding='utf-8') as f:
                return json.load(f)
        elif file_format == "yaml":
            with open(file_path, "r", encoding='utf-8') as f:
                return yaml.safe_load(f)
        elif file_format == "toml":
            with open(file_path, "r", encoding='utf-8') as f:
                return toml.load(f)
        else:
            raise ValueError(f"Unsupported file format: {file_format}")
        
    def save_file(self, file_path: str, file_format: str, data: Any):
        """
        Saves data to a file.
        """
        if file_format == "json":
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)   
        elif file_format == "yaml":
            with open(file_path, 'w', encoding='utf-8') as f:
                yaml.dump(data, f)
        elif file_format == "toml":
            with open(file_path, 'w', encoding='utf-8') as f:
                toml.dump(data, f)


