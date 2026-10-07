"""白名单主机的 DNS 缓存（stale-while-revalidate，持久化到磁盘）。

部分网络环境（例如多个失效的虚拟网卡 DNS）下，系统解析一次需要 10 秒以上，
而 arXiv 与 LLM 服务的 DNS TTL 只有数秒到数十秒，几乎每次运行都会撞上。
这里对白名单主机直接返回上次的解析结果，并在后台线程刷新；
其余主机完全走系统解析，不受影响。
"""

import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

_original_getaddrinfo = socket.getaddrinfo
_lock = threading.Lock()
_hosts: set = set()
_cache: dict = {}
_refreshing: set = set()
_cache_path: Optional[Path] = None

# 在此时长内视为新鲜，不触发后台刷新。
FRESH_SECONDS = 60
# 超过此时长的旧结果不再使用，改为同步解析。
STALE_MAX_SECONDS = 7 * 24 * 3600


def _key(host, port, family, type, proto, flags) -> str:
    # 忽略 proto/flags：aiohttp 与 urllib3 传入的 flags 不同，但解析结果可以共用。
    return json.dumps([host, str(port), int(family), int(type)])


def _encode(results) -> list:
    return [
        [int(family), int(type), int(proto), canonname, list(sockaddr)]
        for family, type, proto, canonname, sockaddr in results
    ]


def _decode(rows) -> list:
    return [
        (socket.AddressFamily(family), socket.SocketKind(type), proto, canonname, tuple(sockaddr))
        for family, type, proto, canonname, sockaddr in rows
    ]


def _save() -> None:
    if _cache_path is None:
        return
    try:
        _cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp = _cache_path.with_suffix(".tmp")
        temp.write_text(json.dumps(_cache), encoding="utf-8")
        os.replace(temp, _cache_path)
    except OSError:
        pass


def _store(key: str, results) -> None:
    with _lock:
        _cache[key] = {"time": time.time(), "results": _encode(results)}
        _save()


def _refresh(key: str, args) -> None:
    try:
        _store(key, _original_getaddrinfo(*args))
    except OSError:
        pass
    finally:
        with _lock:
            _refreshing.discard(key)


def _cached_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if host not in _hosts:
        return _original_getaddrinfo(host, port, family, type, proto, flags)

    args = (host, port, family, type, proto, flags)
    key = _key(*args)
    with _lock:
        entry = _cache.get(key)
    age = time.time() - entry["time"] if entry else None

    if entry and age < STALE_MAX_SECONDS:
        if age >= FRESH_SECONDS:
            with _lock:
                start = key not in _refreshing
                _refreshing.add(key)
            if start:
                threading.Thread(target=_refresh, args=(key, args), daemon=True).start()
        return _decode(entry["results"])

    results = _original_getaddrinfo(*args)
    _store(key, results)
    return results


def install(hosts: Iterable[str], cache_path: Optional[Path] = None) -> None:
    """为给定主机启用缓存；可重复调用以追加主机。"""
    global _cache_path
    with _lock:
        _hosts.update(host for host in hosts if host)
        if _cache_path is None:
            _cache_path = cache_path or Path.home() / ".cache" / "latextrans" / "dns_cache.json"
            try:
                _cache.update(json.loads(_cache_path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
    socket.getaddrinfo = _cached_getaddrinfo


def prewarm(host: str, port: int) -> None:
    """提前解析一次（首次运行、缓存为空时仍有收益）。"""
    try:
        socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except OSError:
        pass
