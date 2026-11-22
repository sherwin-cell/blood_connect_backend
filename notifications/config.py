"""Worker settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class Settings:
    # Set NOTIFICATION_WORKER_ENABLED=false to run only the HTTP API.
    enabled: bool = True
    # Most donors alerted about one newly opened request.
    max_fanout: int = 200
    # How often the worker records that it is alive and checks its listeners.
    heartbeat_seconds: int = 120
    # After a restart, events newer than (last heartbeat - margin) are caught up.
    catchup_margin_seconds: int = 300
    # Pending pushes older than this are not sent (the in-app record remains).
    push_max_age_hours: int = 24

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            enabled=_bool("NOTIFICATION_WORKER_ENABLED", True),
            max_fanout=_int("NOTIFICATION_MAX_FANOUT", 200),
            heartbeat_seconds=_int("NOTIFICATION_HEARTBEAT_SECONDS", 120),
            catchup_margin_seconds=_int("NOTIFICATION_CATCHUP_MARGIN_SECONDS", 300),
            push_max_age_hours=_int("NOTIFICATION_PUSH_MAX_AGE_HOURS", 24),
        )
