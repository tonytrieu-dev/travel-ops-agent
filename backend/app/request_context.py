from contextvars import ContextVar
from uuid import uuid4

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


def set_correlation_id(value: str) -> None:
    _correlation_id.set(value)


def correlation_id() -> str:
    return _correlation_id.get() or str(uuid4())
