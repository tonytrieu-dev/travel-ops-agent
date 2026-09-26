from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


def correlation_id() -> str:
    value = _correlation_id.get()
    if not value:
        value = str(uuid4())
        _correlation_id.set(value)
    return value


@contextmanager
def bind_correlation_id(value: str | None = None):
    token = _correlation_id.set(value or str(uuid4()))
    try:
        yield _correlation_id.get()
    finally:
        _correlation_id.reset(token)
