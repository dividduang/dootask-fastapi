from dataclasses import dataclass, field
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any


def utcnow():
    """Database dates are naive UTC; wire format retains source's second precision."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class RequestContext:
    request_id: str
    language: str = "zh"
    platform: str = "web"
    version: str = ""
    fd: str = ""
    token: str = ""
    ip: str = ""
    user_agent: str = ""
    base_url: str = ""
    actor: Any = None
    pgp: dict = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


current_context: ContextVar[RequestContext] = ContextVar("request_context")
