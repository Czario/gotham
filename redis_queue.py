"""Shared Redis queue helpers for the sec-scraper-worker."""
from __future__ import annotations

import json
from typing import Any

from redis import Redis


def get_redis_client(redis_url: str) -> Redis:
    # socket_timeout=None is required for BLPOP: the server-side timeout controls
    # when the command returns; a finite socket timeout races with it and raises
    # TimeoutError before BLPOP can return None cleanly.
    # socket_connect_timeout=10 still limits the initial connection attempt.
    return Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_timeout=None,
        socket_connect_timeout=10,
    )


def decode_message(raw_message: str) -> dict[str, Any]:
    return json.loads(raw_message)


def normalize_queue_name(queue_name: str) -> str:
    queue = queue_name.strip()
    if not queue:
        raise ValueError("queue_name must not be empty")
    return queue
