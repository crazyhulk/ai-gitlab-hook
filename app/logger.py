import logging
import logging.handlers
import os
from contextvars import ContextVar
from pathlib import Path

_request_id: ContextVar[str] = ContextVar("request_id", default="-")
_request_path: ContextVar[str] = ContextVar("request_path", default="-")


def get_request_id() -> str:
    return _request_id.get()


def set_request_id(rid: str) -> None:
    _request_id.set(rid)


def get_request_path() -> str:
    return _request_path.get()


def set_request_path(path: str) -> None:
    _request_path.set(path)


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()      # type: ignore[attr-defined]
        record.request_path = _request_path.get()  # type: ignore[attr-defined]
        return True


_LOG_FORMAT = (
    "%(asctime)s.%(msecs)03d %(levelname)-8s"
    " [%(request_id)s]"
    " [%(filename)s:%(lineno)d]"
    " [%(request_path)s]"
    " %(message)s"
)
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str, log_dir: str, log_file: str) -> None:
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
    ctx_filter = _ContextFilter()

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 避免重复添加 handler（uvicorn reload 场景）
    if root.handlers:
        root.handlers.clear()

    sh = logging.StreamHandler()
    sh.setFormatter(formatter)
    sh.addFilter(ctx_filter)
    root.addHandler(sh)

    if log_dir and log_file:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.TimedRotatingFileHandler(
            os.path.join(log_dir, log_file),
            when="midnight",
            backupCount=30,
            encoding="utf-8",
        )
        fh.setFormatter(formatter)
        fh.addFilter(ctx_filter)
        root.addHandler(fh)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
