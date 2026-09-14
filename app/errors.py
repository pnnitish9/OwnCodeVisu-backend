"""
app/errors.py

Every error the API can return - client mistakes, execution failures, and
unexpected server faults - goes through this module so the response shape is
always the same:

    {
        "success": false,
        "error": {
            "category": "validation" | "syntax" | "runtime" | "timeout" |
                         "resource_limit" | "compile" | "tool_unavailable" |
                         "rate_limited" | "internal",
            "type": "<ExceptionClassName or short machine code>",
            "message": "<human readable summary>",
            "line": <int|null>,
            "column": <int|null>,
            "hint": "<short actionable suggestion|null>",
            "details": "<optional extra text, e.g. traceback tail|null>"
        }
    }

This lets the frontend render a single, consistent error panel regardless of
whether Python, C++, or the web layer itself is what failed.
"""
import logging
import traceback

from flask import jsonify, request

logger = logging.getLogger("codeviz")


class ApiError(Exception):
    """Base class for all errors that should be turned into a clean JSON
    response instead of leaking a stack trace to the client."""

    http_status = 500
    category = "internal"

    def __init__(self, message, *, type_="Error", line=None, column=None,
                 hint=None, details=None, http_status=None):
        super().__init__(message)
        self.message = message
        self.type = type_
        self.line = line
        self.column = column
        self.hint = hint
        self.details = details
        if http_status is not None:
            self.http_status = http_status

    def to_dict(self):
        return {
            "category": self.category,
            "type": self.type,
            "message": self.message,
            "line": self.line,
            "column": self.column,
            "hint": self.hint,
            "details": self.details,
        }


class ValidationError(ApiError):
    """The request itself was malformed (bad JSON, missing fields, code too
    long, unsupported language, etc.). Always the client's fault -> 400."""
    http_status = 400
    category = "validation"

    def __init__(self, message, **kwargs):
        kwargs.setdefault("type_", "ValidationError")
        super().__init__(message, **kwargs)


class PayloadTooLargeError(ApiError):
    http_status = 413
    category = "validation"

    def __init__(self, message="Request body is too large."):
        super().__init__(message, type_="PayloadTooLarge")


class RateLimitError(ApiError):
    http_status = 429
    category = "rate_limited"

    def __init__(self, retry_after_seconds=None):
        message = "Too many requests. Please slow down and try again shortly."
        super().__init__(message, type_="RateLimited")
        self.retry_after_seconds = retry_after_seconds

    def to_dict(self):
        d = super().to_dict()
        d["retry_after_seconds"] = self.retry_after_seconds
        return d


class SyntaxProblem(ApiError):
    """Code failed to parse/compile before it ever ran."""
    http_status = 422
    category = "syntax"

    def __init__(self, message, **kwargs):
        kwargs.setdefault("type_", "SyntaxError")
        super().__init__(message, **kwargs)


class CompileError(ApiError):
    """C++ compilation failed. `details` typically holds the full compiler
    diagnostic list (already parsed into structured entries by the caller,
    attached separately in the response, not here)."""
    http_status = 422
    category = "compile"

    def __init__(self, message, **kwargs):
        kwargs.setdefault("type_", "CompileError")
        super().__init__(message, **kwargs)


class RuntimeProblem(ApiError):
    """Code parsed/compiled fine but raised/crashed while executing."""
    http_status = 200  # this is an expected, well-formed outcome of tracing
    category = "runtime"

    def __init__(self, message, **kwargs):
        kwargs.setdefault("type_", "RuntimeError")
        super().__init__(message, **kwargs)


class ExecutionTimeout(ApiError):
    http_status = 200
    category = "timeout"

    def __init__(self, message="Execution timed out.", **kwargs):
        kwargs.setdefault("type_", "TimeoutError")
        kwargs.setdefault(
            "hint",
            "Check for infinite loops or recursion without a base case. "
            "Large loops may also simply exceed the step limit.",
        )
        super().__init__(message, **kwargs)


class ResourceLimitExceeded(ApiError):
    http_status = 200
    category = "resource_limit"

    def __init__(self, message, **kwargs):
        kwargs.setdefault("type_", "ResourceLimitExceeded")
        super().__init__(message, **kwargs)


class ToolUnavailableError(ApiError):
    """A required external tool (g++, gdb, ...) is missing on the server."""
    http_status = 503
    category = "tool_unavailable"

    def __init__(self, message, **kwargs):
        kwargs.setdefault("type_", "ToolUnavailable")
        super().__init__(message, **kwargs)


def register_error_handlers(app):
    @app.errorhandler(ApiError)
    def handle_api_error(err: ApiError):
        if err.http_status >= 500:
            logger.error("API error [%s]: %s", err.type, err.message, exc_info=True)
        else:
            logger.info("API error [%s]: %s", err.type, err.message)
        return jsonify({"success": False, "error": err.to_dict()}), err.http_status

    @app.errorhandler(413)
    def handle_413(_err):
        return handle_api_error(PayloadTooLargeError())

    @app.errorhandler(415)
    def handle_415(_err):
        return handle_api_error(ValidationError(
            "Content-Type must be application/json.",
            type_="UnsupportedMediaType",
            http_status=415,
        ))

    @app.errorhandler(404)
    def handle_404(_err):
        return handle_api_error(ValidationError(
            f"No such endpoint: {request.method} {request.path}",
            type_="NotFound",
            http_status=404,
        ))

    @app.errorhandler(405)
    def handle_405(_err):
        return handle_api_error(ValidationError(
            f"Method {request.method} not allowed for {request.path}.",
            type_="MethodNotAllowed",
            http_status=405,
        ))

    @app.errorhandler(Exception)
    def handle_uncaught(err: Exception):
        # Last line of defense: something we did not anticipate blew up.
        # Never leak internals to the client; log the full traceback instead.
        logger.error("Unhandled exception: %s\n%s", err, traceback.format_exc())
        fallback = ApiError(
            "An unexpected server error occurred. This has been logged.",
            type_=type(err).__name__,
            http_status=500,
        )
        return jsonify({"success": False, "error": fallback.to_dict()}), 500
