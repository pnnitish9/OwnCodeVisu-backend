# app/cpp_engine/compiler.py
import logging
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import List, Optional

from .diagnostics import parse_gxx_output

logger = logging.getLogger("codeviz")

_HARNESS_PATH = os.path.join(os.path.dirname(__file__), "harness.cpp")


@dataclass
class CompileResult:
    success: bool
    binary_path: Optional[str] = None
    work_dir: Optional[str] = None
    diagnostics: List[dict] = field(default_factory=list)
    raw_stderr: str = ""
    error: Optional[str] = None  # set for things that aren't compiler diagnostics
    # e.g. g++ missing entirely, or the compiler process itself timed out.


def gxx_available(gxx_path: str) -> bool:
    return shutil.which(gxx_path) is not None


def compile_cpp(code: str, gxx_path: str, timeout_seconds: float) -> CompileResult:
    """Write `code` to a temp .cpp file, link it with the exception-reporting
    harness, and compile with debug info at -O0 (required for a faithful,
    steppable line-by-line trace later)."""
    if not gxx_available(gxx_path):
        return CompileResult(
            success=False,
            error=(
                f"The C++ compiler ('{gxx_path}') was not found on the server. "
                "Install a C++17-capable compiler, e.g. `sudo apt-get install g++`, "
                "or set GXX_PATH to its location."
            ),
        )

    work_dir = tempfile.mkdtemp(prefix="codeviz_cpp_")
    source_path = os.path.join(work_dir, "user_code.cpp")
    harness_copy_path = os.path.join(work_dir, "harness.cpp")
    binary_path = os.path.join(work_dir, "program")

    try:
        with open(source_path, "w", encoding="utf-8") as f:
            f.write(code)
        shutil.copyfile(_HARNESS_PATH, harness_copy_path)

        cmd = [
            gxx_path,
            "-g", "-O0",                 # debug info, no optimization -> trustworthy stepping
            "-std=c++17",
            "-fno-omit-frame-pointer",
            "-Wall", "-Wextra",           # surface likely-bug warnings too
            "-o", binary_path,
            source_path, harness_copy_path,
        ]
        try:
            proc = subprocess.run(
                cmd, cwd=work_dir, capture_output=True, text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            cleanup_work_dir(work_dir)
            return CompileResult(
                success=False,
                error=f"Compilation did not finish within {timeout_seconds:g}s and was aborted.",
            )
        except OSError as exc:
            cleanup_work_dir(work_dir)
            return CompileResult(success=False, error=f"Failed to invoke the compiler: {exc}")

        diagnostics = parse_gxx_output(proc.stderr)

        if proc.returncode != 0 or not os.path.exists(binary_path):
            cleanup_work_dir(work_dir)
            return CompileResult(
                success=False,
                diagnostics=diagnostics or [{
                    "file": "user_code.cpp", "line": None, "column": None,
                    "severity": "error",
                    "message": proc.stderr.strip() or "Compilation failed with no diagnostic output.",
                    "hint": None, "context": [],
                }],
                raw_stderr=proc.stderr,
            )

        os.chmod(binary_path, os.stat(binary_path).st_mode | stat.S_IEXEC)
        return CompileResult(
            success=True, binary_path=binary_path, work_dir=work_dir,
            diagnostics=diagnostics,  # may still contain warnings even on success
            raw_stderr=proc.stderr,
        )
    except Exception:
        cleanup_work_dir(work_dir)
        raise


def cleanup_work_dir(work_dir: str):
    try:
        shutil.rmtree(work_dir, ignore_errors=True)
    except Exception:
        logger.warning("Failed to clean up temp dir %s", work_dir, exc_info=True)
