import logging
import sys

# Streamlit 在没有 ScriptRunContext 的线程里调用 st.* 时会记录
# "missing ScriptRunContext" 警告。过去的做法是全局替换 sys.stderr，
# 这在多线程下会相互覆盖（并泄漏文件句柄、破坏 GUI 的日志重定向）。
# 这里改为只给 Streamlit 对应的 logger 加过滤器，线程安全且不影响其它输出。
_STREAMLIT_CONTEXT_LOGGERS = (
    "streamlit.runtime.scriptrunner_utils.script_run_context",
    "streamlit.runtime.scriptrunner.script_run_context",
)


class _MissingScriptRunContextFilter(logging.Filter):
    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        return "ScriptRunContext" not in message


_context_filter = _MissingScriptRunContextFilter()


def silence_streamlit_context_warnings():
    """幂等：屏蔽 Streamlit 的 missing ScriptRunContext 警告。"""
    for name in _STREAMLIT_CONTEXT_LOGGERS:
        logger = logging.getLogger(name)
        if _context_filter not in logger.filters:
            logger.addFilter(_context_filter)


silence_streamlit_context_warnings()


class _LinePrinter:
    _last_len = 0
    _active = False

    @classmethod
    def update(cls, message):
        text = "" if message is None else str(message)
        pad = max(0, cls._last_len - len(text))
        sys.stdout.write("\r" + text + (" " * pad))
        sys.stdout.flush()
        cls._last_len = len(text)
        cls._active = True

    @classmethod
    def finish(cls):
        if cls._active:
            sys.stdout.write("\n")
            sys.stdout.flush()
        cls._last_len = 0
        cls._active = False


class _ProgressBar:
    def __init__(self, value=0):
        self.value = value

    def progress(self, value, text=None):
        self.value = value
        ratio = value
        if isinstance(value, (int, float)):
            if value > 1:
                ratio = value / 100.0
            ratio = max(0.0, min(1.0, float(ratio)))
            width = 24
            filled = int(width * ratio)
            bar = "[" + ("#" * filled) + ("-" * (width - filled)) + "]"
            percent = f"{ratio * 100:5.1f}%"
            msg = f"{bar} {percent}"
            if text:
                msg = f"{msg} {text}"
            _LinePrinter.update(msg)
        elif text:
            _LinePrinter.update(text)
        return self


class _Status:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def text(self, message):
        _LinePrinter.update(message)

    def success(self, message):
        _LinePrinter.finish()
        print(message)

    def error(self, message):
        _LinePrinter.finish()
        print(message)

    def progress(self, value=0, text=None):
        bar = _ProgressBar(value=value)
        if text:
            _LinePrinter.update(text)
        return bar

    def empty(self):
        _LinePrinter.finish()
        return None


class _CliProgress:
    @staticmethod
    def empty():
        return _Status()

    @staticmethod
    def progress(value=0, text=None):
        bar = _ProgressBar(value=value)
        if text:
            _LinePrinter.update(text)
        return bar

    @staticmethod
    def success(message):
        _LinePrinter.finish()
        print(message)

    @staticmethod
    def error(message):
        _LinePrinter.finish()
        print(message)


_backend = _CliProgress()


class _ProgressProxy:
    def __getattr__(self, name):
        return getattr(_backend, name)


def set_progress_backend(backend):
    global _backend
    silence_streamlit_context_warnings()
    _backend = backend


def get_progress_backend():
    return _backend


st = _ProgressProxy()
