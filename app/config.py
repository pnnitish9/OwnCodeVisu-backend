"""
app/config.py

All tunable limits live here and are loaded from environment variables so the
service can be re-tuned per-deployment without touching code. Every value has
a safe, conservative default.
"""
import os


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _list_env(name: str, default: list) -> list:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


class Config:
    ENV = os.environ.get("FLASK_ENV", "production")
    DEBUG = ENV == "development"
    HOST = os.environ.get("HOST", "0.0.0.0")
    PORT = _int_env("PORT", 5000)

    # A request body larger than this is rejected with 413 before any JSON
    # parsing happens (protects against memory-exhaustion via huge uploads).
    MAX_CONTENT_LENGTH = _int_env("MAX_CONTENT_LENGTH_BYTES", 2 * 1024 * 1024)  # 2MB

    CORS_ORIGINS = _list_env("CORS_ORIGINS", ["*"])

    # ---- Python engine ----
    PY_MAX_CODE_LENGTH = _int_env("PY_MAX_CODE_LENGTH", 20000)
    
    # Default trace limits (conservative for general use)
    PY_MAX_STEPS = _int_env("PY_MAX_STEPS", 15000)
    PY_TIMEOUT_SECONDS = _float_env("PY_TIMEOUT_SECONDS", 6)
    PY_MAX_OUTPUT_CHARS = _int_env("PY_MAX_OUTPUT_CHARS", 200000)
    PY_MAX_MEMORY_MB = _int_env("PY_MAX_MEMORY_MB", 256)
    PY_MAX_USER_INPUTS = _int_env("PY_MAX_USER_INPUTS", 200)
    
    # Extended limits for complex programs (can be requested via API)
    # These are absolute maximums that even explicit requests cannot exceed
    PY_MAX_STEPS_EXTENDED = _int_env("PY_MAX_STEPS_EXTENDED", 50000)
    PY_TIMEOUT_SECONDS_EXTENDED = _float_env("PY_TIMEOUT_SECONDS_EXTENDED", 20)
    PY_MAX_MEMORY_MB_EXTENDED = _int_env("PY_MAX_MEMORY_MB_EXTENDED", 512)
    
    # Smart tracing optimizations
    PY_ENABLE_SMART_SNAPSHOTS = os.environ.get("PY_ENABLE_SMART_SNAPSHOTS", "true").lower() == "true"
    PY_SNAPSHOT_INTERVAL = _int_env("PY_SNAPSHOT_INTERVAL", 50)  # Full heap snapshot every N steps
    PY_MAX_HEAP_OBJECTS = _int_env("PY_MAX_HEAP_OBJECTS", 2000)
    PY_ENABLE_STEP_SAMPLING = os.environ.get("PY_ENABLE_STEP_SAMPLING", "false").lower() == "true"
    PY_RECURSION_DEPTH_THRESHOLD = _int_env("PY_RECURSION_DEPTH_THRESHOLD", 10)  # When to start sampling

    # ---- C++ engine ----
    CPP_MAX_CODE_LENGTH = _int_env("CPP_MAX_CODE_LENGTH", 20000)
    CPP_MAX_STEPS = _int_env("CPP_MAX_STEPS", 4000)
    CPP_COMPILE_TIMEOUT_SECONDS = _float_env("CPP_COMPILE_TIMEOUT_SECONDS", 10)
    CPP_RUN_TIMEOUT_SECONDS = _float_env("CPP_RUN_TIMEOUT_SECONDS", 8)
    CPP_MAX_OUTPUT_CHARS = _int_env("CPP_MAX_OUTPUT_CHARS", 200000)
    CPP_MAX_MEMORY_MB = _int_env("CPP_MAX_MEMORY_MB", 256)
    GXX_PATH = os.environ.get("GXX_PATH", "g++")
    GDB_PATH = os.environ.get("GDB_PATH", "gdb")

    # ---- Rate limiting (simple in-memory token bucket, see app/security.py) ----
    RATE_LIMIT_REQUESTS = _int_env("RATE_LIMIT_REQUESTS", 30)
    RATE_LIMIT_WINDOW_SECONDS = _int_env("RATE_LIMIT_WINDOW_SECONDS", 60)


config = Config()
