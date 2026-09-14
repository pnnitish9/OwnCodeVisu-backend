"""
app/security.py

Two things live here:

1. `get_json_body` / `require_code_field` - defensive request parsing so a
   malformed request always fails with a clear ValidationError instead of an
   unhandled TypeError/KeyError deep in a route.

2. `RateLimiter` - a small in-memory sliding-window limiter keyed by client
   IP. It is intentionally dependency-free so the project runs with nothing
   more than Flask installed. It is per-process state, which is fine for a
   single-worker/self-hosted deployment; for multi-worker production
   deployments, put a real reverse-proxy rate limiter (nginx, Cloudflare,
   flask-limiter + Redis, etc.) in front instead and treat this as a backstop.
"""
import threading
import time
from collections import defaultdict, deque

from flask import request

from .errors import RateLimitError, ValidationError


def get_json_body():
    """Parse the request body as JSON, raising a clean ValidationError for
    every way that can go wrong (missing body, invalid JSON, wrong content
    type, not a JSON object)."""
    if not request.data:
        raise ValidationError("Request body is empty. Expected a JSON object.")

    try:
        data = request.get_json(force=False, silent=False)
    except Exception as exc:  # werkzeug raises a BadRequest subclass
        raise ValidationError(f"Request body is not valid JSON: {exc}") from exc

    if data is None:
        raise ValidationError("Request body is empty. Expected a JSON object.")
    if not isinstance(data, dict):
        raise ValidationError("Request body must be a JSON object.")
    return data


def require_string_field(data: dict, field: str, *, max_length=None, allow_empty=False):
    if field not in data:
        raise ValidationError(f"Missing required field: '{field}'.")
    value = data[field]
    if not isinstance(value, str):
        raise ValidationError(f"Field '{field}' must be a string.")
    if not allow_empty and not value.strip():
        raise ValidationError(f"Field '{field}' must not be empty.")
    if max_length is not None and len(value) > max_length:
        raise ValidationError(
            f"Field '{field}' is too long ({len(value)} characters, "
            f"limit is {max_length}).",
            type_="PayloadTooLarge",
        )
    return value


class RateLimiter:
    """Fixed-window-ish limiter: allow `max_requests` per `window_seconds`
    per client key. Thread-safe; O(1) amortized per request."""

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str):
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            cutoff = now - self.window_seconds
            while hits and hits[0] < cutoff:
                hits.popleft()
            if len(hits) >= self.max_requests:
                retry_after = max(0.0, self.window_seconds - (now - hits[0]))
                raise RateLimitError(retry_after_seconds=round(retry_after, 1))
            hits.append(now)

    def client_key(self) -> str:
        # Respect a trusted proxy header if present, else fall back to the
        # socket address. This is best-effort identification, not auth.
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.remote_addr or "unknown"
