"""限流（HTTP 429）与服务端过载时的退避和自适应并发控制。

- ``backoff_delay``：指数退避 + 抖动，优先遵守 ``Retry-After``。
- ``AdaptiveConcurrencyLimiter``：AIMD 风格的并发闸门。收到限流信号后
  并发上限减半并进入冷却期；之后每成功完成一轮请求再逐步恢复。
- 冷却截止时间按接口地址在线程间共享：多个项目并发翻译时打到同一个
  服务商，一个项目被限流后其它项目也应暂缓发送新请求。
"""
import asyncio
import collections
import random
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Deque, Dict, Optional

RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
# 这两类状态说明请求过多，需要降低并发；502/504 只做单请求退避。
THROTTLE_STATUSES = frozenset({429, 503})

_COOLDOWN_LOCK = threading.Lock()
_COOLDOWN_UNTIL: Dict[str, float] = {}


def set_shared_cooldown(key: Optional[str], seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
    if not key or seconds <= 0:
        return
    until = clock() + seconds
    with _COOLDOWN_LOCK:
        if until > _COOLDOWN_UNTIL.get(key, 0.0):
            _COOLDOWN_UNTIL[key] = until


def shared_cooldown_remaining(key: Optional[str], clock: Callable[[], float] = time.monotonic) -> float:
    if not key:
        return 0.0
    with _COOLDOWN_LOCK:
        until = _COOLDOWN_UNTIL.get(key, 0.0)
    return max(0.0, until - clock())


def parse_retry_after(headers: Any, now: Optional[float] = None) -> Optional[float]:
    """解析 ``Retry-After``（秒数或 HTTP 日期）；无法解析时返回 None。"""
    if not headers:
        return None
    try:
        value = headers.get("Retry-After") or headers.get("retry-after")
    except AttributeError:
        return None
    if value is None:
        return None
    value = str(value).strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    current = time.time() if now is None else now
    return max(0.0, target - current)


def backoff_delay(
    attempt: int,
    base: float,
    cap: float,
    retry_after: Optional[float] = None,
    rng: Callable[[], float] = random.random,
) -> float:
    """第 ``attempt`` 次（从 0 开始）重试前的等待秒数。

    没有 ``Retry-After`` 时采用“等值抖动”：在 [d/2, d] 内随机，
    d = min(cap, base * 2**attempt)；避免大量请求同时醒来再次撞上限流。
    有 ``Retry-After`` 时等待该时长（最多 max(cap, 120) 秒，防止异常值
    卡死整篇翻译），再加少量抖动错开请求。
    """
    base = max(0.0, base)
    cap = max(base, cap)
    if retry_after is not None:
        jitter = rng() * min(1.0, base if base > 0 else 1.0)
        return min(retry_after, max(cap, 120.0)) + jitter
    delay = min(cap, base * (2 ** max(0, attempt)))
    return delay / 2 + rng() * delay / 2


class AdaptiveConcurrencyLimiter:
    """单个事件循环内使用的 AIMD 并发闸门（不跨线程使用）。"""

    def __init__(
        self,
        max_limit: int,
        min_limit: int = 1,
        cooldown_key: Optional[str] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_limit = max(1, int(max_limit))
        self.min_limit = max(1, min(int(min_limit), self.max_limit))
        self.limit = self.max_limit
        self.active = 0
        self.cooldown_key = cooldown_key
        self._clock = clock
        self._local_cooldown_until = 0.0
        self._last_decrease = float("-inf")
        self._decrease_window = 0.0
        self._successes = 0
        self._waiters: Deque[asyncio.Future] = collections.deque()

    def cooldown_remaining(self) -> float:
        local = max(0.0, self._local_cooldown_until - self._clock())
        return max(local, shared_cooldown_remaining(self.cooldown_key, self._clock))

    @property
    def throttled(self) -> bool:
        return self.limit < self.max_limit or self.cooldown_remaining() > 0

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            remaining = self.cooldown_remaining()
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            if self.active < self.limit:
                self.active += 1
                return
            waiter = loop.create_future()
            self._waiters.append(waiter)
            try:
                await waiter
            except asyncio.CancelledError:
                if waiter.done() and not waiter.cancelled():
                    # 已被唤醒却被取消：把名额让给下一个等待者。
                    self._wake()
                else:
                    try:
                        self._waiters.remove(waiter)
                    except ValueError:
                        pass
                raise

    def release(self) -> None:
        self.active = max(0, self.active - 1)
        self._wake()

    def _wake(self) -> None:
        free = self.limit - self.active
        while free > 0 and self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                free -= 1

    def on_success(self) -> None:
        """加性增：每成功完成约一轮（limit 个）请求，上限增加约 1/8。"""
        if self.limit >= self.max_limit:
            return
        self._successes += 1
        if self._successes >= self.limit:
            self._successes = 0
            self.limit = min(self.max_limit, self.limit + max(1, self.limit // 8))
            self._wake()

    def on_throttle(self, cooldown: float) -> bool:
        """乘性减：同一冷却窗口内的多次限流只减半一次。返回是否真的下调。"""
        now = self._clock()
        cooldown = max(0.0, cooldown)
        self._local_cooldown_until = max(self._local_cooldown_until, now + cooldown)
        set_shared_cooldown(self.cooldown_key, cooldown, self._clock)
        if now < self._last_decrease + self._decrease_window:
            return False
        self._last_decrease = now
        self._decrease_window = max(1.0, cooldown)
        self._successes = 0
        new_limit = max(self.min_limit, self.limit // 2)
        decreased = new_limit < self.limit
        self.limit = new_limit
        return decreased
