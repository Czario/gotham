"""Shared Redis queue helpers for worker processes."""
from __future__ import annotations

import json
from typing import Any

from redis import Redis


def get_redis_client(redis_url: str) -> Redis:
    """Connect to Redis with infinite socket timeout for BLPOP."""
    return Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_timeout=None,
        socket_connect_timeout=10,
    )


def decode_message(raw_message: str) -> dict[str, Any]:
    """Parse JSON message payload from queue."""
    return json.loads(raw_message)


def normalize_queue_name(queue_name: str) -> str:
    """Validate and strip queue name."""
    queue = queue_name.strip()
    if not queue:
        raise ValueError("queue_name must not be empty")
    return queue
