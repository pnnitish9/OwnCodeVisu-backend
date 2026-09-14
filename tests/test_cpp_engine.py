"""
Run with: pytest tests/test_cpp_engine.py -v

Requires g++ on PATH (skips entirely otherwise). The step-tracing tests are
skipped when gdb isn't installed - compile/run/error-classification tests
still execute either way, since those don't need gdb.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402
from cpp_engine import compiler as compiler_mod  # noqa: E402
from cpp_engine.diagnostics import parse_gxx_output, signal_message, suggest_missing_header  # noqa: E402
from cpp_engine.engine import run_cpp_trace  # noqa: E402

GXX_AVAILABLE = shutil.which("g++") is not None
GDB_AVAILABLE = shutil.which("gdb") is not None

requires_gxx = pytest.mark.skipif(not GXX_AVAILABLE, reason="g++ not installed")
requires_gdb = pytest.mark.skipif(not GDB_AVAILABLE, reason="gdb not installed")

DEFAULT_LIMITS = {
    "gxx_path": "g++", "gdb_path": "gdb",
    "compile_timeout_seconds": 10, "run_timeout_seconds": 5,
    "max_steps": 2000, "max_output_chars": 100000, "max_memory_mb": 256,
}


@requires_gxx
def test_successful_compile_and_run():
    code = '#include <iostream>\nint main() { std::cout << "hi" << std::endl; return 0; }\n'
    outcome = run_cpp_trace(code, "", DEFAULT_LIMITS)
    assert outcome.kind == "success"
    assert "hi" in outcome.payload["data"]["output"]


@requires_gxx
def test_compile_error_has_diagnostics():
    code = "int main() {\n    vector<int> v;\n    v.push_back(1);\n    return 0;\n}\n"
    outcome = run_cpp_trace(code, "", DEFAULT_LIMITS)
    assert outcome.kind == "compile_error"
    assert len(outcome.payload["diagnostics"]) > 0
    assert outcome.payload["diagnostics"][0]["severity"] == "error"


@requires_gxx
def test_segfault_reported_with_signal_info():
    code = "int main() {\n    int* p = nullptr;\n    *p = 1;\n    return 0;\n}\n"
    outcome = run_cpp_trace(code, "", DEFAULT_LIMITS)
    assert outcome.kind == "success"
    steps = outcome.payload["data"]["steps"]
    assert any(s.get("exception_info") for s in steps)
    exc = next(s["exception_info"] for s in steps if s.get("exception_info"))
    assert "Segmentation" in exc["message"] or "Segmentation" in exc["type"]


@requires_gxx
def test_uncaught_exception_reported_via_harness():
    code = (
        "#include <stdexcept>\n"
        'int main() { throw std::runtime_error("boom"); return 0; }\n'
    )
    outcome = run_cpp_trace(code, "", DEFAULT_LIMITS)
    assert outcome.kind == "success"
    steps = outcome.payload["data"]["steps"]
    exc = next(s["exception_info"] for s in steps if s.get("exception_info"))
    assert exc["type"] == "std::runtime_error"
    assert "boom" in exc["message"]


@requires_gxx
def test_tool_unavailable_when_gxx_missing():
    outcome = run_cpp_trace("int main(){return 0;}", "", {**DEFAULT_LIMITS, "gxx_path": "/no/such/g++"})
    assert outcome.kind == "tool_unavailable"


@requires_gxx
@requires_gdb
def test_step_trace_available_with_gdb():
    code = (
        "int main() {\n"
        "    int x = 1;\n"
        "    int y = 2;\n"
        "    int z = x + y;\n"
        "    return 0;\n"
        "}\n"
    )
    outcome = run_cpp_trace(code, "", DEFAULT_LIMITS)
    assert outcome.kind == "success"
    assert outcome.payload["data"]["total_steps"] > 0
    assert outcome.payload["data"].get("no_step_trace") in (False, None)


def test_diagnostic_parsing_basic():
    stderr = (
        "user_code.cpp:3:5: error: 'vector' was not declared in this scope\n"
        "    3 |     vector<int> v;\n"
        "      |     ^~~~~~\n"
    )
    diags = parse_gxx_output(stderr)
    assert len(diags) == 1
    assert diags[0]["line"] == 3
    assert diags[0]["severity"] == "error"
    assert "include <vector>" in diags[0]["hint"]


def test_header_suggestion_smart_quotes():
    hint = suggest_missing_header("\u2018cout\u2019 was not declared in this scope")
    assert hint is not None
    assert "iostream" in hint


def test_signal_message_segv():
    import signal
    type_slug, title, hint = signal_message(signal.SIGSEGV)
    assert type_slug == "SegmentationFault"
    assert "pointer" in hint.lower() or "memory" in hint.lower()
