# app/cpp_engine/diagnostics.py
"""
Classification tables for everything that can go wrong compiling/running C++:

  - SIGNAL_MESSAGES: fatal signal number -> friendly explanation
  - classify_diagnostic: one g++ diagnostic line -> friendly hint
  - suggest_missing_header: "'vector' was not declared" -> "#include <vector>"
"""
import re
import signal as _signal

def _sig(name):
    """Return the signal enum member by name, or None if unsupported on this platform."""
    return getattr(_signal, name, None)


SIGNAL_MESSAGES = {}

_SIGNAL_DEFS = [
    ("SIGSEGV", (
        "SegmentationFault", "Segmentation fault",
        "The program tried to access memory it doesn't own. Common causes: "
        "dereferencing a null or dangling pointer, an out-of-bounds array/"
        "vector access, or a stack overflow from unbounded recursion.",
    )),
    ("SIGFPE", (
        "FloatingPointException", "Floating point exception",
        "Usually an integer division or modulo by zero (e.g. `x % 0`), or "
        "an arithmetic overflow trap.",
    )),
    ("SIGABRT", (
        "ProgramAborted", "Program aborted",
        "Something called abort() - typically an uncaught C++ exception, a "
        "failed assert(), a std::vector::at() out-of-range access, or a "
        "detected heap corruption (double free / buffer overflow).",
    )),
    ("SIGILL", (
        "IllegalInstruction", "Illegal instruction",
        "The CPU tried to execute an invalid instruction, often caused by "
        "calling through a corrupted function pointer or executing "
        "corrupted memory.",
    )),
    ("SIGBUS", (
        "BusError", "Bus error",
        "An invalid memory access, often from misaligned memory access or "
        "accessing a memory-mapped file past its end.",
    )),
    ("SIGKILL", (
        "ProcessKilled", "Process killed",
        "The process was forcibly terminated, most likely by the sandbox's "
        "memory limit (out-of-memory kill) or the hard timeout.",
    )),
]

for _sig_name, _sig_info in _SIGNAL_DEFS:
    _s = _sig(_sig_name)
    if _s is not None:
        SIGNAL_MESSAGES[_s] = _sig_info


def signal_message(signum: int):
    try:
        sig = _signal.Signals(signum)
    except ValueError:
        return f"Signal{signum}", f"Signal {signum}", "The program was terminated by an unrecognized signal."
    type_slug, title, hint = SIGNAL_MESSAGES.get(
        sig, (sig.name, sig.name, "The program was terminated by this signal.")
    )
    return type_slug, title, hint


# --- Compiler diagnostics -------------------------------------------------

_GXX_LINE_RE = re.compile(
    r"^(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<severity>error|warning|note):\s*(?P<message>.*)$"
)

# A small, high-value subset of std:: names mapped to the header that
# declares them - covers the overwhelming majority of "was not declared"
# beginner errors.
_HEADER_SUGGESTIONS = {
    "vector": "<vector>", "string": "<string>", "map": "<map>", "unordered_map": "<unordered_map>",
    "set": "<set>", "unordered_set": "<unordered_set>", "queue": "<queue>", "stack": "<stack>",
    "deque": "<deque>", "pair": "<utility>", "cout": "<iostream>", "cin": "<iostream>",
    "endl": "<iostream>", "cerr": "<iostream>", "sort": "<algorithm>", "max": "<algorithm>",
    "min": "<algorithm>", "swap": "<utility>", "sqrt": "<cmath>", "pow": "<cmath>",
    "abs": "<cstdlib>", "unique_ptr": "<memory>", "shared_ptr": "<memory>", "make_shared": "<memory>",
    "make_unique": "<memory>", "function": "<functional>", "thread": "<thread>", "mutex": "<mutex>",
    "accumulate": "<numeric>", "setprecision": "<iomanip>", "setw": "<iomanip>",
    "ifstream": "<fstream>", "ofstream": "<fstream>", "stringstream": "<sstream>",
    "numeric_limits": "<limits>", "array": "<array>", "tuple": "<tuple>", "optional": "<optional>",
    "variant": "<variant>", "regex": "<regex>", "atomic": "<atomic>",
}

_NOT_DECLARED_RE = re.compile(
    r"[\'\u2018\u2019`]([A-Za-z_][A-Za-z0-9_:]*)[\'\u2018\u2019`] (?:was not declared|is not a member)"
)


def suggest_missing_header(message: str):
    match = _NOT_DECLARED_RE.search(message)
    if not match:
        return None
    name = match.group(1).split("::")[-1]
    header = _HEADER_SUGGESTIONS.get(name)
    if header:
        return f"Did you forget `#include {header}`? (needed for `{name}`)"
    if "std" in message and "::" not in name:
        return f"`{name}` looks like it might need a `#include` for the right standard header, " \
               "or a `using namespace std;` / `std::` prefix."
    return None


def classify_diagnostic(message: str) -> str:
    lower = message.lower()
    if "expected ';'" in lower:
        return "Missing semicolon `;` at the end of the previous statement."
    if "expected '}'" in lower or "expected declaration" in lower:
        return "A brace `{` was likely opened without a matching closing `}`."
    if "was not declared" in lower or "is not a member" in lower:
        return suggest_missing_header(message) or (
            "This name is undeclared here - check spelling, includes, and "
            "that it's in scope (namespace/class)."
        )
    if "no matching function" in lower or "no matching function for call" in lower:
        return "None of the overloads/candidates match these argument types " \
               "- check the number and types of arguments you're passing."
    if "redefinition" in lower:
        return "This name is defined more than once in the same scope."
    if "expected primary-expression" in lower:
        return "The compiler hit unexpected syntax here - often a missing "\
               "operand, stray token, or mismatched parenthesis just before this point."
    if "undefined reference" in lower:
        return "This is a linker error: the function/variable is declared " \
               "but never defined (or its definition isn't linked in) - " \
               "check for a missing implementation or typo'd signature."
    return None


def parse_gxx_output(stderr_text: str, max_diagnostics: int = 50):
    """Parse g++ stderr into a list of structured diagnostics. Lines that
    don't match the standard `file:line:col: severity: message` shape
    (continuation lines, caret markers, etc.) are attached as `context` on
    the preceding diagnostic instead of being dropped."""
    diagnostics = []
    for raw_line in stderr_text.splitlines():
        match = _GXX_LINE_RE.match(raw_line)
        if match:
            if len(diagnostics) >= max_diagnostics:
                continue
            message = match.group("message")
            diagnostics.append({
                "file": match.group("file"),
                "line": int(match.group("line")),
                "column": int(match.group("col")),
                "severity": match.group("severity"),
                "message": message,
                "hint": classify_diagnostic(message),
                "context": [],
            })
        elif diagnostics and raw_line.strip():
            diagnostics[-1]["context"].append(raw_line.rstrip())
    return diagnostics
