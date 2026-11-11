"""A ceiling on how fast one account can call the server.

An agent stuck in a loop, or a leaked token, could otherwise issue requests
as fast as the network allows -- each one a storage read or write that is
billed and, for writes, permanent. The limit is per account and deliberately
generous: it is there to stop a runaway, not to meter normal use.

Counted in this process's memory, per HTTP request (a JSON-RPC batch counts
once). With several server processes each keeps its own count, so the real
ceiling is the limit times the number of processes -- still a ceiling.
"""
import threading
import time
from collections import deque

from fastapi import HTTPException

WINDOW_SECONDS = 60
_HITS: dict = {}
_LOCK = threading.Lock()


def check(key: str, limit: int, now: float = None):
    """Count one request for `key`; raise 429 when over `limit` per minute."""
    if not limit or limit < 1 or not key:
        return
    now = time.monotonic() if now is None else now
    with _LOCK:
        hits = _HITS.setdefault(key, deque())
        while hits and now - hits[0] >= WINDOW_SECONDS:
            hits.popleft()
        if len(hits) >= limit:
            wait = max(1, int(WINDOW_SECONDS - (now - hits[0])) + 1)
            raise HTTPException(
                status_code=429,
                detail=f"Too many requests: the limit is {limit} per minute "
                       f"for one account. Try again in {wait} seconds.",
                headers={"Retry-After": str(wait)})
        hits.append(now)
        # Idle accounts are dropped so the table cannot grow without bound.
        if len(_HITS) > 10_000:
            for other in [k for k, v in _HITS.items() if not v]:
                _HITS.pop(other, None)


def reset():
    with _LOCK:
        _HITS.clear()
