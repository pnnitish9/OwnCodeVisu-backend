# app/cpp_engine/debugger.py
"""
Drives GDB (in batch mode, using its embedded Python API) to single-step
through the user's compiled program and record, for each stopped line in
*their* source file: the line number, the current function, and every
in-scope local/argument with a GDB-pretty-printed display string.

Why GDB and not a hand-rolled ptrace debugger? GDB already knows how to
read DWARF debug info, print STL containers via its bundled pretty-printers,
resolve symbols, and step over library internals - reimplementing any
slice of that reliably is a multi-month project on its own. This module is
"just" the glue: generate a driver script, run it, parse its structured
output.

Known limitations (documented here rather than hidden):
  - Only genuinely local scopes are captured (a step's "locals" mirror the
    innermost block, similar to the Python engine's `frame.f_locals`); a
    full heap/pointer graph like the Python engine builds is out of scope.
  - Explicit call/return classification is a best-effort heuristic based on
    call-stack depth changes between consecutive stops, not a true call/
    return trace event.
  - Incremental stdout capture is best-effort: C++ streams are often fully
    buffered when redirected to a file, so output may only "appear" near
    the end of the trace rather than growing steadily, even though it's
    still fully correct at the final step.
"""
import json
import logging
import os
import shutil
import subprocess
import tempfile
import textwrap

from .diagnostics import signal_message

logger = logging.getLogger("codeviz")

_DRIVER_TEMPLATE = r'''
import gdb
import json
import time

STDOUT_PATH = {stdout_path!r}
STDIN_PATH = {stdin_path!r}
OUTPUT_JSON_PATH = {output_json_path!r}
MAX_STEPS = {max_steps}
TIME_LIMIT = {time_limit}
SOURCE_FILENAME = {source_filename!r}

state = {{
    "steps": [], "truncated": False, "truncation_reason": None,
    "exited": False, "exit_code": None, "signal": None, "error": None,
}}

def on_stop(event):
    if isinstance(event, gdb.SignalEvent):
        try:
            state["signal"] = int(event.stop_signal)
        except Exception:
            state["signal_name"] = str(getattr(event, "stop_signal", "unknown"))

def on_exited(event):
    state["exited"] = True
    state["exit_code"] = getattr(event, "exit_code", 0)

gdb.events.stop.connect(on_stop)
gdb.events.exited.connect(on_exited)

def q(cmd):
    try:
        return gdb.execute(cmd, to_string=True)
    except gdb.error:
        return None

q("set pagination off")
q("set confirm off")
q("set print pretty off")
q("set print elements 80")
q("set print repeats 10")
q("set backtrace limit 200")
for pattern in ("/usr/include/c++/*", "/usr/*/bits/*", "/usr/include/*", "*/c++/*/bits/*"):
    q("skip -gfi " + pattern)

def frame_depth(frame):
    depth = 0
    f = frame
    while f is not None and depth < 500:
        depth += 1
        f = f.older()
    return depth

def format_value(value):
    try:
        t = value.type.strip_typedefs()
        if t.code == gdb.TYPE_CODE_PTR:
            text = str(value)
            try:
                target_code = t.target().strip_typedefs().code
            except Exception:
                target_code = None
            is_char_ptr = target_code == gdb.TYPE_CODE_INT and "char" in str(t.target())
            if is_char_ptr:
                return text[:400]
            try:
                if int(value) == 0:
                    return text + " (nullptr)"
                deref = value.dereference()
                return (text + " -> " + str(deref))[:400]
            except Exception:
                return text
        return str(value)[:400]
    except Exception as e:
        return "<error reading value: %s>" % e

def frame_locals(frame):
    out = {{}}
    try:
        block = frame.block()
    except Exception:
        return out
    seen = set()
    while block is not None:
        try:
            for symbol in block:
                if not (symbol.is_variable or symbol.is_argument):
                    continue
                name = symbol.name
                if not name or name in seen or name.startswith("__"):
                    continue
                seen.add(name)
                try:
                    value = symbol.value(frame)
                    out[name] = {{"type": str(symbol.type), "display": format_value(value)}}
                except Exception as e:
                    out[name] = {{"type": "?", "display": "<unavailable: %s>" % e}}
        except Exception:
            pass
        if block.function is not None:
            break
        block = block.superblock
    return out

q("break main")
run_cmd = "run < " + STDIN_PATH + " > " + STDOUT_PATH + " 2>> " + STDOUT_PATH + ".err"
try:
    gdb.execute(run_cmd)
except gdb.error as e:
    state["error"] = str(e)

start_time = time.time()
step_id = 0
prev_func = None
prev_depth = None

while True:
    if state["exited"] or state["signal"] is not None or state["error"]:
        break
    if step_id >= MAX_STEPS:
        state["truncated"] = True
        state["truncation_reason"] = "step_limit"
        break
    if time.time() - start_time > TIME_LIMIT:
        state["truncated"] = True
        state["truncation_reason"] = "timeout"
        break

    try:
        frame = gdb.selected_frame()
    except gdb.error:
        break

    filename = None
    line_no = -1
    try:
        sal = frame.find_sal()
        if sal and sal.symtab:
            filename = sal.symtab.filename
            line_no = sal.line
    except Exception:
        pass

    if filename and filename.endswith(SOURCE_FILENAME):
        depth = frame_depth(frame)
        func_name = frame.name() or "?"
        if prev_depth is None:
            event = "call"
        elif depth > prev_depth:
            event = "call"
        elif depth < prev_depth:
            event = "return"
        else:
            event = "line"
        prev_func, prev_depth = func_name, depth

        step_id += 1
        state["steps"].append({{
            "step_id": step_id,
            "line": (line_no - 1) if line_no and line_no > 0 else -1,
            "function_name": func_name,
            "event": event,
            "locals": frame_locals(frame),
        }})

    try:
        gdb.execute("step", to_string=True)
    except gdb.error as e:
        state["error"] = str(e)
        break

try:
    with open(OUTPUT_JSON_PATH, "w") as f:
        json.dump(state, f)
except Exception:
    pass

q("quit")
'''


def gdb_available(gdb_path: str) -> bool:
    return shutil.which(gdb_path) is not None


def _apply_resource_limits(max_memory_mb: int, cpu_seconds: float):
    try:
        import resource
    except ImportError:
        return
    try:
        mem_bytes = max_memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
    except Exception:
        pass
    try:
        cpu = max(1, int(cpu_seconds) + 5)  # generous: gdb itself needs headroom too
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    except Exception:
        pass
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:
        pass


def trace_with_gdb(gdb_path: str, binary_path: str, work_dir: str, source_filename: str,
                    user_input: str, max_steps: int, timeout_seconds: float,
                    max_memory_mb: int) -> dict:
    """Returns a dict: {ok, steps, output, stderr_extra, truncated,
    truncation_reason, exited, exit_code, signal_info, error}."""
    stdin_path = os.path.join(work_dir, "stdin.txt")
    stdout_path = os.path.join(work_dir, "stdout.txt")
    output_json_path = os.path.join(work_dir, "trace_output.json")
    driver_path = os.path.join(work_dir, "driver.py")

    with open(stdin_path, "w", encoding="utf-8") as f:
        f.write(user_input)
    open(stdout_path, "w").close()

    driver_src = _DRIVER_TEMPLATE.format(
        stdout_path=stdout_path, stdin_path=stdin_path,
        output_json_path=output_json_path, max_steps=max_steps,
        time_limit=timeout_seconds, source_filename=source_filename,
    )
    with open(driver_path, "w", encoding="utf-8") as f:
        f.write(driver_src)

    cmd = [gdb_path, "--batch", "-q", "--nx", "-x", driver_path, binary_path]
    backstop = timeout_seconds + 6
    try:
        proc = subprocess.run(
            cmd, cwd=work_dir, capture_output=True, text=True,
            timeout=backstop,
            preexec_fn=(lambda: _apply_resource_limits(max_memory_mb, timeout_seconds))
            if os.name == "posix" else None,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": {
            "kind": "hard_timeout",
            "message": f"GDB did not finish within {backstop:.0f}s and was forcibly stopped.",
        }}
    except OSError as exc:
        return {"ok": False, "error": {"kind": "tool_error", "message": f"Failed to invoke gdb: {exc}"}}

    output = _read_file(stdout_path)
    stderr_extra = _read_file(stdout_path + ".err")

    if not os.path.exists(output_json_path):
        # GDB ran but our driver never wrote its report - most likely a
        # gdb-python API mismatch on this system, or the program crashed
        # before `break main` was ever hit (unlikely but possible with
        # stripped/static binaries).
        logger.warning("gdb driver produced no report. gdb stderr: %s", proc.stderr[-2000:])
        return {"ok": False, "error": {
            "kind": "trace_unavailable",
            "message": (
                "The debugger ran but did not produce a step-by-step trace "
                "(this can happen with unusual GDB builds). Falling back to "
                "running the program directly."
            ),
            "gdb_stderr": proc.stderr[-2000:],
        }}

    try:
        with open(output_json_path, "r", encoding="utf-8") as f:
            report = json.load(f)
    except Exception as exc:
        return {"ok": False, "error": {"kind": "trace_unavailable", "message": f"Could not read trace report: {exc}"}}

    signal_info = None
    if report.get("signal") is not None:
        type_slug, title, hint = signal_message(report["signal"])
        signal_info = {"signal": report["signal"], "type": type_slug, "title": title, "hint": hint}

    return {
        "ok": True,
        "steps": report.get("steps", []),
        "output": output,
        "stderr_extra": stderr_extra,
        "truncated": report.get("truncated", False),
        "truncation_reason": report.get("truncation_reason"),
        "exited": report.get("exited", False),
        "exit_code": report.get("exit_code"),
        "signal_info": signal_info,
        "driver_error": report.get("error"),
    }


def run_without_gdb(binary_path: str, work_dir: str, user_input: str,
                     timeout_seconds: float, max_memory_mb: int, max_output_chars: int) -> dict:
    """Fallback used when gdb isn't installed: just run the compiled
    program directly so the user at least gets output + a crash diagnosis,
    with no step-by-step trace."""
    try:
        proc = subprocess.run(
            [binary_path], cwd=work_dir, input=user_input,
            capture_output=True, text=True, timeout=timeout_seconds + 2,
            preexec_fn=(lambda: _apply_resource_limits(max_memory_mb, timeout_seconds))
            if os.name == "posix" else None,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "output": (exc.stdout or "")[:max_output_chars] if exc.stdout else "",
            "error": {"kind": "timeout", "message": f"The program did not finish within {timeout_seconds:g}s."},
        }
    except OSError as exc:
        return {"ok": False, "output": "", "error": {"kind": "tool_error", "message": str(exc)}}

    output = (proc.stdout or "")[:max_output_chars]
    signal_info = None
    if proc.returncode < 0:
        type_slug, title, hint = signal_message(-proc.returncode)
        signal_info = {"signal": -proc.returncode, "type": type_slug, "title": title, "hint": hint}

    return {
        "ok": True,
        "output": output,
        "stderr": (proc.stderr or "")[:max_output_chars],
        "exit_code": proc.returncode if proc.returncode >= 0 else None,
        "signal_info": signal_info,
    }


def _read_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""
