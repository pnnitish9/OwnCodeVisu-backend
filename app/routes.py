# app/routes.py
import logging
import shutil
import time

from flask import Blueprint, jsonify

from .config import config
from .errors import (
    CompileError, ExecutionTimeout, ResourceLimitExceeded, RuntimeProblem,
    SyntaxProblem, ToolUnavailableError, ValidationError,
)
from .python_engine import sandbox as py_sandbox
from .python_engine.error_classifier import syntax_hint
from .cpp_engine.engine import run_cpp_trace
from .security import RateLimiter, get_json_body, require_string_field

logger = logging.getLogger("codeviz")
bp = Blueprint("api", __name__)

_rate_limiter = RateLimiter(config.RATE_LIMIT_REQUESTS, config.RATE_LIMIT_WINDOW_SECONDS)
_start_time = time.monotonic()


def _rate_limit():
    _rate_limiter.check(_rate_limiter.client_key())


@bp.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "uptime_seconds": round(time.monotonic() - _start_time, 1),
    })


@bp.route("/api/languages", methods=["GET"])
def languages():
    """Lets the frontend show accurate capability/availability info instead
    of guessing (e.g. gray out C++ tracing if gdb isn't installed)."""
    gxx_ok = shutil.which(config.GXX_PATH) is not None
    gdb_ok = shutil.which(config.GDB_PATH) is not None
    return jsonify({
        "success": True,
        "data": {
            "python": {"available": True, "trace": True},
            "cpp": {
                "available": gxx_ok,
                "trace": gxx_ok and gdb_ok,
                "compiler_found": gxx_ok,
                "debugger_found": gdb_ok,
                "note": None if gdb_ok else (
                    "g++ found, but gdb is not installed - C++ code can be "
                    "compiled and run, but step-by-step tracing is disabled."
                    if gxx_ok else
                    "Neither g++ nor gdb were found on the server."
                ),
            },
        },
    })


@bp.route("/trace", methods=["POST"])
def trace_python_legacy():
    """Backwards-compatible alias for the original single-endpoint API."""
    return trace_python()


@bp.route("/api/python/trace", methods=["POST"])
def trace_python():
    _rate_limit()
    data = get_json_body()
    code = require_string_field(data, "code", max_length=config.PY_MAX_CODE_LENGTH)
    user_input = data.get("user_input", "")
    if not isinstance(user_input, str):
        raise ValidationError("Field 'user_input' must be a string.")
    if len(user_input) > 50000:
        raise ValidationError("Field 'user_input' is too long.", type_="PayloadTooLarge")

    limits = {
        "max_steps": config.PY_MAX_STEPS,
        "timeout_seconds": config.PY_TIMEOUT_SECONDS,
        "max_output_chars": config.PY_MAX_OUTPUT_CHARS,
        "max_memory_mb": config.PY_MAX_MEMORY_MB,
    }

    outcome = py_sandbox.run_traced(code, user_input, limits)

    if outcome.ok:
        return jsonify({"success": True, "data": outcome.result})

    kind = outcome.error["kind"]
    if kind == "syntax_error":
        payload = outcome.error["payload"]
        hint = syntax_hint(payload["type"])
        raise SyntaxProblem(
            f'{payload["type"]}: {payload["msg"]}',
            type_=payload["type"], line=payload.get("lineno"), column=payload.get("offset"),
            hint=hint,
        )
    if kind == "memory_error":
        raise ResourceLimitExceeded(
            "The program exceeded the memory limit and was stopped.",
            hint="Check for unbounded data structures (e.g. a list that "
                 "grows forever inside a loop).",
        )
    if kind == "hard_timeout":
        raise ExecutionTimeout(outcome.error["message"])
    if kind == "killed":
        raise ResourceLimitExceeded(outcome.error["message"])
    if kind == "internal_error":
        payload = outcome.error["payload"]
        logger.error("Internal tracer error: %s\n%s", payload["message"], payload.get("traceback", ""))
        raise RuntimeProblem(
            "The tracer hit an internal error processing this program.",
            type_=payload["type"], details=None,
        )
    raise RuntimeProblem("Unknown execution failure.")


@bp.route("/api/cpp/trace", methods=["POST"])
def trace_cpp():
    _rate_limit()
    data = get_json_body()
    code = require_string_field(data, "code", max_length=config.CPP_MAX_CODE_LENGTH)
    user_input = data.get("user_input", "")
    if not isinstance(user_input, str):
        raise ValidationError("Field 'user_input' must be a string.")

    limits = {
        "gxx_path": config.GXX_PATH,
        "gdb_path": config.GDB_PATH,
        "compile_timeout_seconds": config.CPP_COMPILE_TIMEOUT_SECONDS,
        "run_timeout_seconds": config.CPP_RUN_TIMEOUT_SECONDS,
        "max_steps": config.CPP_MAX_STEPS,
        "max_output_chars": config.CPP_MAX_OUTPUT_CHARS,
        "max_memory_mb": config.CPP_MAX_MEMORY_MB,
    }

    outcome = run_cpp_trace(code, user_input, limits)

    if outcome.kind == "tool_unavailable":
        raise ToolUnavailableError(outcome.payload["message"])
    if outcome.kind == "compile_error":
        diagnostics = outcome.payload["diagnostics"]
        first_error = next((d for d in diagnostics if d["severity"] == "error"), diagnostics[0])
        err = CompileError(
            first_error["message"], line=first_error.get("line"), column=first_error.get("column"),
            hint=first_error.get("hint"),
        )
        response = jsonify({
            "success": False,
            "error": err.to_dict(),
            "diagnostics": diagnostics,
        })
        return response, err.http_status
    # success
    return jsonify({"success": True, "data": outcome.payload["data"]})
