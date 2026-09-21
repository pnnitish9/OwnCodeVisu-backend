# tracer/core.py
"""
The core step-by-step Python tracer.

Compared to the original implementation this version adds three independent
safety nets so a user's program can never hang or exhaust the server:

1. A wall-clock deadline checked on every traced line/call (`timeout_seconds`).
2. A hard cap on the number of recorded steps (`max_steps`).
3. A hard cap on captured stdout size (`max_output_chars`).

Hitting any of these does NOT throw an opaque error - the trace so far is
still returned, with a `truncated` flag and a human-readable reason, so the
user can see exactly where things went off the rails (e.g. "stuck" inside an
infinite loop) instead of getting nothing.
"""
import sys
import time
import inspect
import traceback
from io import StringIO
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field

from .heap_manager import HeapManager
from .formatters import ValueFormatter

DEFAULT_MAX_STEPS = 15000
DEFAULT_TIMEOUT_SECONDS = 6
DEFAULT_MAX_OUTPUT_CHARS = 200000


class _StepLimitExceeded(Exception):
    """Internal control-flow signal, never shown raw to the user."""


class _ExecutionTimedOut(Exception):
    """Internal control-flow signal, never shown raw to the user."""


class _OutputLimitExceeded(Exception):
    """Internal control-flow signal, never shown raw to the user."""


class _BoundedWriter(StringIO):
    """A StringIO that refuses to grow past `limit` characters, raising
    `_OutputLimitExceeded` instead of silently consuming unbounded memory
    (e.g. `while True: print("x" * 10**7)`)."""

    def __init__(self, limit: int):
        super().__init__()
        self._limit = limit
        self._written = 0

    def write(self, s: str) -> int:
        self._written += len(s)
        if self._written > self._limit:
            # Write what we can of the tail, then stop the program.
            allowed = max(0, self._limit - (self._written - len(s)))
            if allowed:
                super().write(s[:allowed])
            raise _OutputLimitExceeded(
                f"Program output exceeded the {self._limit:,} character limit."
            )
        return super().write(s)


@dataclass
class ExecutionStep:
    """Single execution step with advanced tracking"""
    step_id: int
    line: int
    code: str
    function_name: str
    event: str  # 'line', 'call', 'return', 'exception', 'yield', 'class_init'
    stack_locals: Dict[str, Any] = field(default_factory=dict)
    globals_used: Dict[str, Any] = field(default_factory=dict)
    heap_state: Dict[int, Dict] = field(default_factory=dict)
    stdout: str = ""
    changed_vars: List[str] = field(default_factory=list)
    class_name: Optional[str] = None
    is_generator: bool = False
    generator_state: Optional[str] = None
    decorator_chain: List[str] = field(default_factory=list)
    exception_info: Optional[Dict] = None


class AdvancedPythonTracer:
    """Enhanced Python tracer supporting classes, generators, decorators,
    with built-in resource guards."""

    def __init__(self, max_steps: int = DEFAULT_MAX_STEPS,
                 timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                 max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS):
        self.max_steps = max_steps
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars
        self._reset_state()

    def _reset_state(self):
        self.steps: List[ExecutionStep] = []
        self.heap = HeapManager()
        self.formatter = ValueFormatter(self.heap)
        self.source_lines = []
        self.step_counter = 0
        self.previous_state = {}
        self.call_stack = []
        self.class_context = None
        self.generator_trackers = {}
        self.decorator_stack = []
        self.truncated = False
        self.truncation_reason = None
        self._deadline = None

    def trace(self, code: str, user_input: str = "") -> Dict:
        """Trace execution with full feature support. Raises SyntaxError
        (uncaught) if the code doesn't even compile - callers are expected
        to catch that separately, since it happens before any step exists."""
        self._reset_state()
        self.source_lines = code.strip("\n").split("\n")
        self._deadline = time.monotonic() + self.timeout_seconds

        old_stdout = sys.stdout
        old_stdin = sys.stdin
        bounded_out = _BoundedWriter(self.max_output_chars)
        sys.stdout = bounded_out
        if user_input:
            sys.stdin = StringIO(user_input)

        try:
            # Compile separately so a SyntaxError is unambiguous and never
            # gets confused with a runtime error raised by user code that
            # happens to construct a SyntaxError instance itself.
            compiled = compile(code, "<string>", "exec")
            namespace = {"__builtins__": self._get_full_builtins(), "__name__": "__main__"}
            sys.settrace(self._trace_callback)
            try:
                exec(compiled, namespace)
            finally:
                sys.settrace(None)

        except SyntaxError:
            # Must happen before the traced program ran at all - restore
            # stdio (via the `finally` below) and let the caller handle it.
            raise
        except _StepLimitExceeded:
            self.truncated = True
            self.truncation_reason = "step_limit"
            self._append_truncation_step(
                f"Trace stopped after {self.max_steps:,} steps to protect "
                "the server. This usually means an infinite (or very long) "
                "loop."
            )
        except _ExecutionTimedOut:
            self.truncated = True
            self.truncation_reason = "timeout"
            self._append_truncation_step(
                f"Execution stopped after exceeding the {self.timeout_seconds:g}s "
                "time limit. This usually means an infinite loop or a very "
                "slow computation."
            )
        except _OutputLimitExceeded as exc:
            self.truncated = True
            self.truncation_reason = "output_limit"
            self._append_truncation_step(str(exc))
        except SystemExit:
            pass  # sys.exit() inside traced code - not an error
        except Exception as exc:
            self._capture_exception(exc)
        finally:
            output = bounded_out.getvalue()
            sys.stdout = old_stdout
            sys.stdin = old_stdin
            sys.settrace(None)

        return {
            "steps": [self._step_to_dict(s) for s in self.steps],
            "heap": self.heap.get_all_objects(),
            "output": output,
            "total_steps": len(self.steps),
            "source_code": self.source_lines,
            "generators": self.generator_trackers,
            "truncated": self.truncated,
            "truncation_reason": self.truncation_reason,
            "heap_truncated": self.heap.truncated,
        }

    def _append_truncation_step(self, message: str):
        self.step_counter += 1
        self.steps.append(ExecutionStep(
            step_id=self.step_counter,
            line=-1,
            code=f"⚠ {message}",
            function_name=self.call_stack[-1] if self.call_stack else "<module>",
            event="exception",
            exception_info={"type": "ExecutionLimit", "message": message},
        ))

    def _check_limits(self):
        self.step_counter += 1
        if self.step_counter > self.max_steps:
            raise _StepLimitExceeded()
        # Checking the clock every step is cheap relative to everything
        # else happening per line (formatting, heap traversal).
        if time.monotonic() > self._deadline:
            raise _ExecutionTimedOut()

    def _trace_callback(self, frame, event, arg):
        if frame.f_code.co_filename != "<string>":
            return self._trace_callback

        if event == "line":
            self._check_limits()
            self._capture_line_step(frame)
        elif event == "call":
            self._check_limits()
            self._capture_call_step(frame, arg)
        elif event == "return":
            self._capture_return_step(frame, arg)
        elif event == "exception":
            self._capture_exception_step(frame, arg)

        return self._trace_callback

    def _capture_line_step(self, frame):
        line_no = frame.f_lineno

        if "self" in frame.f_locals and self.class_context is None:
            self.class_context = type(frame.f_locals["self"]).__name__
        elif "cls" in frame.f_locals:
            try:
                self.class_context = frame.f_locals["cls"].__name__
            except Exception:
                pass

        stack_locals = {}
        for name, value in frame.f_locals.items():
            if name.startswith("__"):
                continue
            formatted = self.formatter.format_value(value, name)
            if hasattr(value, "__class__"):
                formatted["class"] = value.__class__.__name__
            if name == "self" and hasattr(value, "__dict__"):
                try:
                    formatted["attributes"] = {
                        attr: self.formatter.format_value(val, attr)
                        for attr, val in list(value.__dict__.items())[:100]
                        if not attr.startswith("__")
                    }
                except Exception:
                    pass
            stack_locals[name] = formatted

        current_state = self._hash_state(stack_locals)
        changed_vars = [
            var for var in current_state
            if var not in self.previous_state or self.previous_state[var] != current_state[var]
        ]
        self.previous_state = current_state

        is_generator = False
        generator_state = None
        for var_name, value in frame.f_locals.items():
            try:
                if inspect.isgenerator(value):
                    is_generator = True
                    gen_id = id(value)
                    if gen_id not in self.generator_trackers:
                        self.generator_trackers[gen_id] = {
                            "name": var_name, "created_at_step": self.step_counter, "yields": [],
                        }
                    generator_state = "suspended"
                elif inspect.isgeneratorfunction(value):
                    generator_state = "defined"
            except Exception:
                continue

        code_line = ""
        if 0 < line_no <= len(self.source_lines):
            code_line = self.source_lines[line_no - 1].strip()

        self.steps.append(ExecutionStep(
            step_id=self.step_counter,
            line=line_no - 1,
            code=code_line,
            function_name=self.call_stack[-1] if self.call_stack else "<module>",
            event="line",
            stack_locals=stack_locals,
            heap_state=self.heap.get_all_objects(),
            changed_vars=changed_vars,
            class_name=self.class_context,
            is_generator=is_generator,
            generator_state=generator_state,
            decorator_chain=list(self.decorator_stack),
        ))

    def _capture_call_step(self, frame, arg):
        func_name = frame.f_code.co_name
        self.call_stack.append(func_name)

        if func_name == "wrapper" or "wrapper" in func_name.lower():
            self.decorator_stack.append(func_name)

        is_method = "self" in frame.f_locals
        class_name = None
        if is_method:
            try:
                class_name = type(frame.f_locals["self"]).__name__
            except Exception:
                pass

        args = {}
        for name, value in frame.f_locals.items():
            if not name.startswith("__"):
                args[name] = self.formatter.format_value(value, name)

        self.steps.append(ExecutionStep(
            step_id=self.step_counter,
            line=frame.f_lineno - 1,
            code=f"→ Calling {func_name}(...)",
            function_name=func_name,
            event="call",
            stack_locals=args,
            heap_state=self.heap.get_all_objects(),
            changed_vars=[],
            class_name=class_name,
            decorator_chain=list(self.decorator_stack),
        ))

    def _capture_return_step(self, frame, arg):
        self.step_counter += 1
        func_name = self.call_stack.pop() if self.call_stack else "<unknown>"

        if self.decorator_stack and func_name == "wrapper":
            self.decorator_stack.pop()

        return_value = self.formatter.format_value(arg, "__return__")

        self.steps.append(ExecutionStep(
            step_id=self.step_counter,
            line=frame.f_lineno - 1,
            code=f"← Returning from {func_name}",
            function_name=func_name,
            event="return",
            stack_locals={"__return__": return_value},
            heap_state=self.heap.get_all_objects(),
            changed_vars=["__return__"],
            class_name=self.class_context,
            decorator_chain=list(self.decorator_stack),
        ))

        if func_name == "__init__":
            self.class_context = None

    def _capture_exception_step(self, frame, arg):
        exc_type, exc_value, _exc_tb = arg
        # Skip our own internal control-flow signals - they aren't user
        # errors and are handled separately by the outer try/except in
        # trace().
        if exc_type in (_StepLimitExceeded, _ExecutionTimedOut, _OutputLimitExceeded):
            return
        self.steps.append(ExecutionStep(
            step_id=self.step_counter,
            line=frame.f_lineno - 1,
            code=f"Exception: {exc_type.__name__}: {exc_value}",
            function_name=self.call_stack[-1] if self.call_stack else "<module>",
            event="exception",
            exception_info={"type": exc_type.__name__, "message": str(exc_value), "line": frame.f_lineno},
        ))

    def _capture_exception(self, exception):
        self.step_counter += 1
        from .error_classifier import classify_exception
        info = classify_exception(exception)
        info["traceback"] = self._clean_traceback(exception)
        self.steps.append(ExecutionStep(
            step_id=self.step_counter,
            line=-1,
            code=f"Program terminated: {type(exception).__name__}: {exception}",
            function_name=self.call_stack[-1] if self.call_stack else "<module>",
            event="exception",
            exception_info=info,
        ))

    def _clean_traceback(self, exception: BaseException) -> str:
        """Render a traceback that only shows frames from the user's own
        code (filename '<string>'), hiding the tracer's own call frames
        which would otherwise be confusing noise."""
        try:
            tb_lines = traceback.format_exception(type(exception), exception, exception.__traceback__)
            relevant = [line for line in tb_lines if '<string>' in line or not line.startswith('  File')]
            text = "".join(relevant) if relevant else "".join(tb_lines)
        except Exception:
            text = f"{type(exception).__name__}: {exception}"
        if len(text) > 4000:
            text = text[:4000] + "\n... (truncated)"
        return text

    def _hash_state(self, state: Dict) -> Dict:
        return {k: str(v.get("value", v.get("heap_ref", ""))) for k, v in state.items()}

    def _get_full_builtins(self):
        """Return a builtins dict for the sandboxed namespace with the
        clearly dangerous, I/O-capable, or sandbox-escaping names removed.

        This is a defense-in-depth measure, NOT a hard security boundary -
        pure-Python attribute introspection (e.g. walking
        `().__class__.__base__.__subclasses__()`) can still reach many
        objects even without `__import__`/`open`. The real security
        boundary is process isolation + OS resource limits, applied by
        `sandbox.run_traced` around this call. See the project README's
        "Security model" section.
        """
        import builtins
        safe = {
            name: getattr(builtins, name)
            for name in dir(builtins)
            if not name.startswith("_") or name in {"__import__", "__build_class__"}
        }
        dangerous = {
            "exec", "eval", "compile", "open",
            "input",  # stdin is supplied up-front via user_input instead
            "breakpoint", "memoryview", "vars", "globals", "locals",
        }
        for func in dangerous:
            safe.pop(func, None)

        # Re-add a scripted `input()` that reads from the pre-supplied
        # stdin StringIO, so `input()`-using programs still work.
        def _scripted_input(prompt=""):
            if prompt:
                sys.stdout.write(str(prompt))
            line = sys.stdin.readline()
            if line == "":
                raise EOFError("EOF when reading a line (no more input provided)")
            return line.rstrip("\n")

        safe["input"] = _scripted_input
        
        # Add a restricted __import__ that allows safe standard library modules
        ALLOWED_MODULES = {
            # Data structures
            'collections', 'array', 'heapq', 'bisect', 'queue', 'enum',
            # Math & numbers
            'math', 'cmath', 'decimal', 'fractions', 'random', 'statistics',
            # Strings & text
            'string', 're', 'difflib', 'textwrap',
            # Dates & times
            'datetime', 'calendar', 'time',
            # Iterators
            'itertools', 'functools', 'operator',
            # Algorithms
            'copy', 'pprint',
            # Data persistence (safe subset)
            'json', 'base64', 'struct', 'codecs',
            # Functional programming
            'typing',
        }
        
        def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
            # Get the top-level module name (before any dots)
            top_level = name.split('.')[0]
            
            if top_level not in ALLOWED_MODULES:
                raise ImportError(
                    f"Import of '{name}' is not allowed in the sandbox. "
                    f"Only these modules are available: {', '.join(sorted(ALLOWED_MODULES))}"
                )
            
            return builtins.__import__(name, globals, locals, fromlist, level)
        
        safe["__import__"] = _safe_import
        return safe

    def _step_to_dict(self, step: ExecutionStep) -> Dict:
        result = {
            "step_id": step.step_id,
            "line": step.line,
            "code": step.code,
            "function_name": step.function_name,
            "event": step.event,
            "stack_locals": step.stack_locals,
            "heap_state": step.heap_state,
            "changed_vars": step.changed_vars,
        }
        if step.class_name:
            result["class_name"] = step.class_name
        if step.is_generator:
            result["is_generator"] = True
        if step.generator_state:
            result["generator_state"] = step.generator_state
        if step.decorator_chain:
            result["decorator_chain"] = step.decorator_chain
        if step.exception_info:
            result["exception_info"] = step.exception_info
        return result
