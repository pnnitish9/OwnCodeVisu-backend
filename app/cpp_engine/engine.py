# app/cpp_engine/engine.py
"""
Top-level entry point for the C++ engine: `run_cpp_trace(code, user_input,
limits)` -> a dict shaped as closely as possible like the Python engine's
result, so the frontend can reuse most of its rendering logic across both
languages. See README "Response schema" for the exact contract.
"""
import logging
import re

from . import compiler as compiler_mod
from . import debugger as debugger_mod

logger = logging.getLogger("codeviz")

_UNCAUGHT_MARKER = re.compile(r"__CODEVIZ_UNCAUGHT_EXCEPTION__:1")
_EXC_TYPE_MARKER = re.compile(r"__CODEVIZ_EXC_TYPE__:(.*)")
_EXC_WHAT_MARKER = re.compile(r"__CODEVIZ_EXC_WHAT__:(.*)")

SOURCE_FILENAME = "user_code.cpp"


class CppEngineOutcome:
    """Discriminated result: exactly one of the three kinds is populated."""

    def __init__(self, kind: str, **payload):
        assert kind in ("compile_error", "tool_unavailable", "success")
        self.kind = kind
        self.payload = payload


def _extract_harness_markers(stderr_text: str):
    if not _UNCAUGHT_MARKER.search(stderr_text):
        return None
    type_match = _EXC_TYPE_MARKER.search(stderr_text)
    what_match = _EXC_WHAT_MARKER.search(stderr_text)
    return {
        "exception_type": type_match.group(1).strip() if type_match else "unknown",
        "exception_what": what_match.group(1).strip() if what_match else "",
    }


def _build_exception_info_from_signal(signal_info, harness_info):
    if harness_info and signal_info and signal_info.get("signal") is not None:
        # SIGABRT caused by an uncaught C++ exception - report the real
        # exception instead of the generic "program aborted" message.
        return {
            "type": harness_info["exception_type"],
            "message": harness_info["exception_what"] or "(no message)",
            "category": "runtime",
            "hint": "This exception was thrown somewhere in the traced "
                    "program and never caught. Wrap the call in a "
                    "try/catch, or fix the condition that triggers it.",
        }
    if signal_info:
        return {
            "type": signal_info["type"],
            "message": signal_info["title"],
            "category": "runtime",
            "hint": signal_info["hint"],
        }
    return None


def run_cpp_trace(code: str, user_input: str, limits: dict) -> CppEngineOutcome:
    compile_result = compiler_mod.compile_cpp(code, limits["gxx_path"], limits["compile_timeout_seconds"])

    if compile_result.error and not compile_result.diagnostics:
        return CppEngineOutcome("tool_unavailable", message=compile_result.error)

    if not compile_result.success:
        return CppEngineOutcome(
            "compile_error",
            diagnostics=compile_result.diagnostics,
            raw_stderr=compile_result.raw_stderr[:8000],
        )

    work_dir = compile_result.work_dir
    binary_path = compile_result.binary_path
    warnings = [d for d in compile_result.diagnostics if d.get("severity") == "warning"]

    try:
        if not debugger_mod.gdb_available(limits["gdb_path"]):
            fallback = debugger_mod.run_without_gdb(
                binary_path, work_dir, user_input,
                limits["run_timeout_seconds"], limits["max_memory_mb"], limits["max_output_chars"],
            )
            return _outcome_from_no_gdb_fallback(fallback, warnings, code, no_gdb=True)

        trace = debugger_mod.trace_with_gdb(
            limits["gdb_path"], binary_path, work_dir, SOURCE_FILENAME, user_input,
            limits["max_steps"], limits["run_timeout_seconds"], limits["max_memory_mb"],
        )

        if not trace["ok"]:
            # GDB itself failed to produce a report (rare - unusual GDB
            # build, sandboxing issue, etc). Don't give up: fall back to
            # running the binary directly so the user still gets output.
            logger.warning("gdb tracing failed (%s), falling back to direct run", trace["error"]["kind"])
            fallback = debugger_mod.run_without_gdb(
                binary_path, work_dir, user_input,
                limits["run_timeout_seconds"], limits["max_memory_mb"], limits["max_output_chars"],
            )
            return _outcome_from_no_gdb_fallback(
                fallback, warnings, code, no_gdb=False,
                degraded_reason=trace["error"]["message"],
            )

        return _outcome_from_gdb_trace(trace, warnings, code)
    finally:
        compiler_mod.cleanup_work_dir(work_dir)


def _outcome_from_gdb_trace(trace: dict, warnings: list, code: str) -> CppEngineOutcome:
    harness_info = _extract_harness_markers(trace.get("stderr_extra", ""))
    exception_info = None

    if trace.get("signal_info") is not None:
        exception_info = _build_exception_info_from_signal(trace["signal_info"], harness_info)
    elif harness_info:
        exception_info = {
            "type": harness_info["exception_type"],
            "message": harness_info["exception_what"] or "(no message)",
            "category": "runtime",
            "hint": "This exception was thrown and never caught.",
        }
    elif trace.get("exit_code") not in (0, None):
        exception_info = {
            "type": "NonZeroExit",
            "message": f"The program exited with status code {trace['exit_code']}.",
            "category": "runtime",
            "hint": "A non-zero exit code often signals an error condition "
                    "the program detected itself (e.g. `return 1;` from main).",
        }

    steps = _convert_gdb_steps(trace["steps"], code)
    if exception_info:
        steps.append(_synthetic_exception_step(steps, exception_info))

    return CppEngineOutcome(
        "success",
        data={
            "steps": steps,
            "heap": {},
            "output": trace.get("output", ""),
            "total_steps": len(steps),
            "source_code": code.split("\n"),
            "generators": {},
            "truncated": trace.get("truncated", False),
            "truncation_reason": trace.get("truncation_reason"),
            "heap_truncated": False,
            "compiler_warnings": warnings,
            "engine": "cpp_gdb",
        },
    )


def _outcome_from_no_gdb_fallback(fallback: dict, warnings: list, code: str, *, no_gdb: bool,
                                   degraded_reason: str = None) -> CppEngineOutcome:
    if not fallback["ok"]:
        err = fallback["error"]
        message = err["message"]
        if err["kind"] == "timeout":
            category, etype = "timeout", "TimeoutError"
        else:
            category, etype = "runtime", "ExecutionError"
        exception_info = {"type": etype, "message": message, "category": category, "hint": None}
        return CppEngineOutcome(
            "success",
            data={
                "steps": [_synthetic_exception_step([], exception_info)],
                "heap": {}, "output": fallback.get("output", ""), "total_steps": 1,
                "source_code": code.split("\n"), "generators": {},
                "truncated": False, "truncation_reason": None, "heap_truncated": False,
                "compiler_warnings": warnings, "engine": "cpp_no_gdb",
                "no_step_trace": True,
                "no_step_trace_reason": (
                    "gdb is not installed on the server, so only compilation "
                    "and program output are available (no step-by-step trace)."
                    if no_gdb else
                    f"Step-by-step tracing failed ({degraded_reason}); showing "
                    "direct program output instead."
                ),
            },
        )

    harness_info = _extract_harness_markers(fallback.get("stderr", ""))
    exception_info = None
    if fallback.get("signal_info"):
        exception_info = _build_exception_info_from_signal(fallback["signal_info"], harness_info)
    elif harness_info:
        exception_info = {
            "type": harness_info["exception_type"],
            "message": harness_info["exception_what"] or "(no message)",
            "category": "runtime", "hint": "This exception was thrown and never caught.",
        }
    elif fallback.get("exit_code") not in (0, None):
        exception_info = {
            "type": "NonZeroExit",
            "message": f"The program exited with status code {fallback['exit_code']}.",
            "category": "runtime", "hint": None,
        }

    steps = [_synthetic_exception_step([], exception_info)] if exception_info else []
    reason = (
        "gdb is not installed on the server, so only compilation and program "
        "output are available (no step-by-step trace). Install gdb "
        "(`sudo apt-get install gdb`) to enable full tracing."
        if no_gdb else
        f"Step-by-step tracing failed ({degraded_reason}); showing direct "
        "program output instead."
    )
    return CppEngineOutcome(
        "success",
        data={
            "steps": steps, "heap": {}, "output": fallback.get("output", ""),
            "total_steps": len(steps), "source_code": code.split("\n"), "generators": {},
            "truncated": False, "truncation_reason": None, "heap_truncated": False,
            "compiler_warnings": warnings, "engine": "cpp_no_gdb",
            "no_step_trace": True, "no_step_trace_reason": reason,
        },
    )


def _convert_gdb_steps(raw_steps: list, code: str) -> list:
    source_lines = code.split("\n")
    out = []
    for s in raw_steps:
        line_idx = s.get("line", -1)
        code_line = source_lines[line_idx].strip() if 0 <= line_idx < len(source_lines) else ""
        stack_locals = {}
        for name, info in (s.get("locals") or {}).items():
            stack_locals[name] = {
                "type": info.get("type", "?"),
                "value": info.get("display", ""),
                "display": info.get("display", ""),
            }
        out.append({
            "step_id": s["step_id"],
            "line": line_idx,
            "code": code_line,
            "function_name": s.get("function_name", "?"),
            "event": s.get("event", "line"),
            "stack_locals": stack_locals,
            "heap_state": {},
            "changed_vars": [],
        })
    return out


def _synthetic_exception_step(existing_steps: list, exception_info: dict) -> dict:
    next_id = (existing_steps[-1]["step_id"] + 1) if existing_steps else 1
    return {
        "step_id": next_id,
        "line": -1,
        "code": f"Program terminated: {exception_info['type']}: {exception_info['message']}",
        "function_name": existing_steps[-1]["function_name"] if existing_steps else "<program>",
        "event": "exception",
        "stack_locals": {},
        "heap_state": {},
        "changed_vars": [],
        "exception_info": exception_info,
    }
