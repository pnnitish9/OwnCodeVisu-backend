# app/python_engine/worker.py
"""
Standalone sandbox worker, run as `python -m app.python_engine.worker` (see
sandbox.py). It is a plain OS subprocess launched via `subprocess.run`, not
Python's `multiprocessing` module - deliberately: `multiprocessing`'s
"spawn" start method re-imports the launching process's __main__ module to
bootstrap the child, which gets fragile (or outright breaks with a
RuntimeError) depending on how the parent Flask app was started - a
one-off script, an interactive shell, a WSGI server, a debugger, etc. A
plain subprocess of a real, `-m`-importable module sidesteps all of that
and behaves identically everywhere.

Protocol: reads one JSON object from stdin -
    {"code": str, "user_input": str, "limits": {...}}
writes one JSON object to stdout -
    {"kind": "ok" | "syntax_error" | "memory_error" | "internal_error",
     "payload": ...}
Any OS-level resource limiting (RLIMIT_AS/RLIMIT_CPU) is applied by the
*caller* via subprocess `preexec_fn`, before this script's code even runs,
since that has to happen before the interpreter finishes starting up to be
airtight.
"""
import json
import sys
import traceback


def main():
    raw = sys.stdin.read()
    try:
        request = json.loads(raw)
        code = request["code"]
        user_input = request.get("user_input", "")
        limits = request["limits"]
    except Exception as exc:
        json.dump({"kind": "internal_error", "payload": {
            "type": "WorkerProtocolError",
            "message": f"Malformed request to sandbox worker: {exc}",
            "traceback": "",
        }}, sys.stdout)
        return

    try:
        from .tracer import AdvancedPythonTracer
        tracer = AdvancedPythonTracer(
            max_steps=limits["max_steps"],
            timeout_seconds=limits["timeout_seconds"],
            max_output_chars=limits["max_output_chars"],
        )
        result = tracer.trace(code, user_input)
        json.dump({"kind": "ok", "payload": result}, sys.stdout)
    except SyntaxError as exc:
        json.dump({"kind": "syntax_error", "payload": {
            "msg": exc.msg, "lineno": exc.lineno, "offset": exc.offset,
            "type": type(exc).__name__,
        }}, sys.stdout)
    except MemoryError:
        json.dump({"kind": "memory_error", "payload": None}, sys.stdout)
    except BaseException as exc:  # noqa: BLE001 - final safety net inside the worker
        json.dump({"kind": "internal_error", "payload": {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }}, sys.stdout)


if __name__ == "__main__":
    main()
