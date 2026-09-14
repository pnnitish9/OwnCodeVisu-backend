#!/usr/bin/env python3
"""
Run this after installing dependencies to sanity-check the environment
before pointing the frontend at the backend:

    python scripts/check_environment.py

It never fails hard for the C++ toolchain - Python tracing works with zero
extra setup, C++ tracing is opportunistic and degrades gracefully (see
README "C++ engine capability levels").
"""
import shutil
import subprocess
import sys


def check(label, ok, detail=""):
    status = "OK  " if ok else "MISS"
    print(f"[{status}] {label}" + (f" - {detail}" if detail else ""))
    return ok


def main():
    print("CodeViz environment check\n" + "-" * 40)
    py_ok = check("Python", True, sys.version.split()[0])

    gxx = shutil.which("g++")
    gxx_ok = check("g++ (C++ compiler)", gxx is not None, gxx or "not found - install with `apt-get install g++`")
    if gxx_ok:
        try:
            out = subprocess.run(["g++", "--std=c++17", "-dM", "-E", "-x", "c++", "/dev/null"],
                                  capture_output=True, text=True, timeout=5)
            cpp17_ok = "__cplusplus" in out.stdout
            check("g++ supports C++17", cpp17_ok)
        except Exception as exc:
            check("g++ supports C++17", False, str(exc))

    gdb = shutil.which("gdb")
    gdb_ok = check("gdb (C++ step tracer)", gdb is not None,
                    gdb or "not found - C++ compiles/runs but has no step trace; install with `apt-get install gdb`")
    if gdb_ok:
        try:
            out = subprocess.run(["gdb", "--batch", "-q", "--nx", "-ex", "python print('py-ok')", "-ex", "quit"],
                                  capture_output=True, text=True, timeout=5)
            python_ok = "py-ok" in out.stdout
            check("gdb built with Python scripting support", python_ok,
                  "" if python_ok else "this GDB build lacks Python support; C++ tracing will fall back to plain execution")
        except Exception as exc:
            check("gdb built with Python scripting support", False, str(exc))

    try:
        import flask  # noqa: F401
        check("Flask installed", True)
    except ImportError:
        check("Flask installed", False, "run: pip install -r requirements.txt")

    print("-" * 40)
    print("Python tracing:", "ready" if py_ok else "NOT ready")
    print("C++ tracing:", "ready" if (gxx_ok and gdb_ok) else
          ("compile+run only (no step trace)" if gxx_ok else "NOT ready"))


if __name__ == "__main__":
    main()
