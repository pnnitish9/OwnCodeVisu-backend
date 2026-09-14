"""
app/python_engine/error_classifier.py

Maps every exception type the traced code can raise onto:
  - a stable `category` (used by the frontend to pick an icon/color)
  - a short, human `hint` (a plain-language nudge toward the fix)

This is deliberately data-driven (one table) so adding support for a new
exception type is a one-line addition, not new branching logic scattered
around the codebase.
"""
import re

# Order matters only for the regex-based `classify_syntax_message` fallback;
# the main table below is a plain dict keyed by exception class name.

_HINTS = {
    "SyntaxError": "Check for a missing colon, unmatched bracket/quote, or "
                   "a stray keyword near the reported line.",
    "IndentationError": "Python is whitespace-sensitive — make sure this "
                         "block's indentation matches the line above it "
                         "and that you aren't mixing tabs and spaces.",
    "TabError": "This file mixes tabs and spaces for indentation. Pick one "
                "(spaces are the Python convention) and use it consistently.",
    "NameError": "A variable or function is being used before it was "
                 "defined, or its name is misspelled.",
    "UnboundLocalError": "A local variable is referenced before it was "
                          "assigned a value inside this function. If you "
                          "meant to use a variable from an outer scope, "
                          "add a `global`/`nonlocal` declaration.",
    "TypeError": "An operation was used on a value of the wrong type — "
                 "e.g. mixing str and int, or calling something that "
                 "isn't callable.",
    "ValueError": "A function received an argument of the right type but "
                  "an inappropriate value (e.g. int('abc')).",
    "ZeroDivisionError": "Division or modulo by zero. Check the denominator "
                          "before dividing.",
    "IndexError": "A sequence (list/tuple/string) was accessed with an "
                  "index outside its valid range. Remember indices go from "
                  "0 to len(seq) - 1.",
    "KeyError": "A dictionary was accessed with a key that doesn't exist. "
                "Use `.get(key, default)` or check `key in dict` first.",
    "AttributeError": "An attribute or method doesn't exist on this "
                       "object — check spelling and that the object is the "
                       "type you expect (it may be `None`).",
    "ImportError": "The module or name being imported isn't available in "
                   "this sandboxed environment.",
    "ModuleNotFoundError": "This module isn't available in the sandboxed "
                            "execution environment. Only the standard "
                            "library subset exposed by the tracer can be "
                            "imported.",
    "RecursionError": "The function called itself too many times without "
                       "reaching a base case — check your recursion's stop "
                       "condition.",
    "MemoryError": "The program tried to use more memory than is allowed "
                    "in this sandbox.",
    "OverflowError": "A numeric result was too large to represent.",
    "StopIteration": "next() was called on an iterator that has no more "
                      "items. If this is inside a generator, this usually "
                      "means the generator function returned.",
    "UnicodeDecodeError": "Bytes could not be decoded using the given text "
                           "encoding.",
    "UnicodeEncodeError": "Text could not be encoded using the given "
                           "encoding — it likely contains characters that "
                           "encoding cannot represent.",
    "FileNotFoundError": "File and network I/O are disabled in this "
                          "sandbox; `open()` is not available.",
    "PermissionError": "File and network I/O are disabled in this sandbox.",
    "AssertionError": "An `assert` statement's condition evaluated to "
                       "False.",
    "NotImplementedError": "This code path explicitly raises "
                            "NotImplementedError — it has not been "
                            "implemented yet.",
    "KeyboardInterrupt": "Execution was interrupted.",
    "FloatingPointError": "A floating point operation failed.",
    "LookupError": "A lookup on a sequence or mapping failed.",
    "ArithmeticError": "An arithmetic operation failed.",
    "RuntimeError": "A generic runtime error was raised by the program or "
                     "one of the functions it called.",
}

_CATEGORY_OVERRIDES = {
    "SyntaxError": "syntax",
    "IndentationError": "syntax",
    "TabError": "syntax",
    "RecursionError": "resource_limit",
    "MemoryError": "resource_limit",
}

_DEFAULT_CATEGORY = "runtime"


def syntax_hint(type_name: str) -> str:
    """Look up the generic hint for a syntax-family exception by class
    name, for callers (like the sandboxed-process boundary) that only have
    the exception's type name and message, not the live instance."""
    return _HINTS.get(type_name, _HINTS["SyntaxError"])


def classify_exception(exc: BaseException) -> dict:
    """Return {category, type, message, hint} for a caught exception
    instance, ready to merge into an API error payload."""
    type_name = type(exc).__name__
    return {
        "type": type_name,
        "category": _CATEGORY_OVERRIDES.get(type_name, _DEFAULT_CATEGORY),
        "message": f"{type_name}: {exc}",
        "hint": _HINTS.get(type_name),
    }


_INDENT_RE = re.compile(r"unexpected indent|unindent does not match", re.I)
_EOF_RE = re.compile(r"unexpected EOF|was never closed", re.I)


def classify_syntax_error(exc: SyntaxError) -> dict:
    """SyntaxError has its own richer fields (lineno, offset, text); build a
    slightly smarter hint by sniffing the CPython message."""
    msg = exc.msg or "invalid syntax"
    hint = _HINTS.get(type(exc).__name__, _HINTS["SyntaxError"])

    if _INDENT_RE.search(msg):
        hint = _HINTS["IndentationError"]
    elif _EOF_RE.search(msg):
        hint = ("A bracket, parenthesis, or quote was opened but never "
                "closed — check for a missing ), ], }, or matching quote.")
    elif "':'" in msg or "expected ':'" in msg:
        hint = "This line looks like it needs a trailing colon `:` " \
               "(e.g. after if/for/while/def/class)."

    return {
        "type": type(exc).__name__,
        "category": "syntax",
        "message": f"{type(exc).__name__}: {msg}",
        "hint": hint,
        "line": exc.lineno,
        "column": exc.offset,
    }
