# app/python_engine/sandbox.py
"""
Runs the Python tracer in an isolated child OS process (see worker.py for
why this is a plain `subprocess`, not `multiprocessing`).

Why a subprocess at all, given the tracer already enforces a soft timeout,
step cap, and output cap in-process (see tracer.py)? Two reasons:

1. Those in-process guards only fire on 'line'/'call' trace events. Code
   that blocks inside a single C-level call (rare with the restricted
   builtin set, but not impossible) would never hit them. A subprocess with
   a hard `timeout=` + kill is a real backstop.
2. `resource.setrlimit` (POSIX) can cap address space and CPU time for the
   child specifically, without affecting the Flask worker itself. If a
   traced program truly exhausts memory, only the child dies.

This is defense in depth, not a full untrusted-code sandbox (see the
"Security model" section of the README). For public, multi-tenant
deployments, run the whole backend inside a container/VM with its own
resource limits (Docker `--memory`/`--cpus`, gVisor, firecracker, etc.) in
addition to this.
"""
import json
import os
import subprocess
import sys

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _apply_resource_limits(max_memory_mb: int, cpu_seconds: float):
    try:
        import resource
    except ImportError:
        return  # Windows: no POSIX rlimits; the subprocess timeout below still applies.
    try:
        mem_bytes = max_memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
    except Exception:
        pass
    try:
        cpu = max(1, int(cpu_seconds) + 2)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    except Exception:
        pass
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:
        pass


class SandboxResult:
    __slots__ = ("ok", "result", "error")

    def __init__(self, ok, result=None, error=None):
        self.ok = ok
        self.result = result
        self.error = error


def run_traced(code: str, user_input: str, limits: dict) -> SandboxResult:
    """Run the tracer in an isolated child process. Never raises for
    expected failure modes (timeout, OOM kill, syntax error, internal bug) -
    all of those come back as a non-ok SandboxResult for the caller to turn
    into a proper API error."""
    request = json.dumps({"code": code, "user_input": user_input, "limits": limits})
    backstop_seconds = limits["timeout_seconds"] + 3

    preexec = None
    if os.name == "posix":
        preexec = lambda: _apply_resource_limits(limits["max_memory_mb"], limits["timeout_seconds"])  # noqa: E731

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "app.python_engine.worker"],
            input=request, capture_output=True, text=True,
            timeout=backstop_seconds, cwd=_BACKEND_ROOT, preexec_fn=preexec,
        )
    except subprocess.TimeoutExpired:
        return SandboxResult(ok=False, error={
            "kind": "hard_timeout",
            "message": (
                f"Execution exceeded the hard {backstop_seconds:.0f}s limit "
                "and had to be forcibly stopped."
            ),
        })
    except OSError as exc:
        return SandboxResult(ok=False, error={
            "kind": "tool_error",
            "message": f"Failed to launch the sandboxed worker process: {exc}",
        })

    if proc.returncode != 0 or not proc.stdout.strip():
        return SandboxResult(ok=False, error={
            "kind": "killed",
            "message": (
                "The sandboxed process exited before it could report a "
                f"result (exit code {proc.returncode}). This usually means "
                "it exceeded the memory limit and was killed by the "
                "operating system."
            ),
            "stderr_tail": (proc.stderr or "")[-2000:],
        })

    try:
        response = json.loads(proc.stdout)
    except Exception as exc:
        return SandboxResult(ok=False, error={
            "kind": "tool_error",
            "message": f"Could not parse sandbox worker output: {exc}",
            "stderr_tail": (proc.stderr or "")[-2000:],
        })

    kind, payload = response.get("kind"), response.get("payload")
    if kind == "ok":
        return SandboxResult(ok=True, result=payload)
    return SandboxResult(ok=False, error={"kind": kind, "payload": payload})
