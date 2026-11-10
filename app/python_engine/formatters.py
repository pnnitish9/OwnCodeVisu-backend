# tracer/formatters.py
"""
Formats a raw Python value into the small JSON-friendly dict the frontend's
variable/heap panels render. Hardened version: every call into user-defined
code (repr, __len__, __iter__, inspect.signature...) is wrapped so a
deliberately or accidentally broken dunder method can't crash the tracer.
"""
from typing import Any, Dict
import inspect

from .heap_manager import HeapManager, MAX_REPR_LEN

MAX_STRING_DISPLAY = 200
MAX_ITER_PREVIEW = 3


def _safe(fn, fallback):
    try:
        return fn()
    except Exception:
        return fallback


class ValueFormatter:
    """Formats Python values with detailed type information for the UI."""

    def __init__(self, heap_manager: HeapManager):
        self.heap = heap_manager

    def format_value(self, value: Any, name: str = "") -> Dict:
        try:
            return self._format_value(value, name)
        except Exception as exc:
            # Absolute last resort - never let a formatting bug take the
            # whole trace down.
            return {"type": type(value).__name__, "value": "<unformattable>",
                    "display": f"<error formatting value: {exc}>"}

    def _format_value(self, value: Any, name: str) -> Dict:
        if value is None:
            return {"type": "NoneType", "value": "None", "display": "None"}

        if isinstance(value, bool):
            return {"type": "bool", "value": str(value), "display": str(value)}
        elif isinstance(value, (int, float, complex)):
            return self._format_number(value)
        elif isinstance(value, str):
            return self._format_string(value)
        elif isinstance(value, bytes):
            return self._format_bytes(value)
        elif inspect.isfunction(value) or inspect.ismethod(value):
            return self._format_function(value, name)
        elif inspect.isclass(value):
            return self._format_class(value, name)
        elif inspect.isgenerator(value):
            return self._format_generator(value, name)
        elif hasattr(value, "__iter__") and hasattr(value, "__next__") and not isinstance(value, (list, tuple, dict, set)):
            return self._format_iterator(value, name)
        elif isinstance(value, (list, tuple, dict, set, frozenset)) or (
            hasattr(value, '__iter__') and type(value).__name__ in ('deque', 'Counter', 'OrderedDict', 'defaultdict')
        ) and not isinstance(value, str):
            return self._format_container(value, name)
        elif hasattr(value, "__dict__"):
            return self._format_object(value, name)
        else:
            return {
                "type": type(value).__name__,
                "value": _safe(lambda: repr(value), "<unrepresentable>"),
                "display": _safe(lambda: repr(value), "<unrepresentable>"),
            }

    def _format_number(self, value):
        try:
            text = str(value)
        except Exception:
            text = "<unrepresentable number>"
        return {"type": type(value).__name__, "value": text, "display": text, "numeric": True}

    def _format_string(self, value):
        display = _safe(lambda: repr(value), "<unrepresentable string>")
        if len(display) > MAX_STRING_DISPLAY:
            display = display[: MAX_STRING_DISPLAY - 3] + "..."
        full_value = value if len(value) <= MAX_REPR_LEN else value[: MAX_REPR_LEN - 3] + "..."
        return {"type": "str", "value": full_value, "display": display, "length": len(value)}

    def _format_bytes(self, value):
        r = _safe(lambda: repr(value), "<unrepresentable bytes>")
        return {"type": "bytes", "value": r, "display": r[:50], "length": len(value)}

    def _format_function(self, value, name):
        sig = _safe(lambda: str(inspect.signature(value)), "(...)")
        fname = getattr(value, "__name__", name or "<function>")
        module = getattr(value, "__module__", "")
        return {"type": "function", "name": fname, "signature": sig,
                "display": f"def {fname}{sig}", "module": module}

    def _format_class(self, value, name):
        bases = _safe(lambda: [b.__name__ for b in value.__bases__ if b is not object], [])
        heap_id = self.heap.add_object(value, name or value.__name__)
        display = f"class {value.__name__}" + (f'({", ".join(bases)})' if bases else "")
        return {"type": "class", "name": value.__name__, "bases": bases, "heap_ref": heap_id, "display": display}

    def _format_generator(self, value, name):
        heap_id = self.heap.add_object(value, name or "generator")
        return {"type": "generator", "heap_ref": heap_id,
                "display": f'<generator object {name or ""}>', "state": "suspended"}

    def _format_iterator(self, value, name):
        # Deliberately do NOT consume the iterator to build a preview -
        # doing so (as the original implementation did) silently mutates
        # the user's program state, producing confusing/incorrect traces.
        return {"type": type(value).__name__, "display": f"<{type(value).__name__} iterator>"}

    def _format_container(self, value, name):
        heap_id = self.heap.add_object(value, name or type(value).__name__)
        preview = self._create_preview(value)
        length = _safe(lambda: len(value), None)
        return {"type": type(value).__name__, "heap_ref": heap_id, "preview": preview,
                "display": preview, "length": length}

    def _format_object(self, value, name):
        heap_id = self.heap.add_object(value, name or type(value).__name__)
        attrs = _safe(lambda: {k: v for k, v in value.__dict__.items() if not k.startswith("_")}, {})
        preview = f"<{type(value).__name__} object>"
        if attrs:
            attr_str = ", ".join(f"{k}={_safe(lambda v=v: repr(v)[:20], '?')}" for k, v in list(attrs.items())[:3])
            if len(attrs) > 3:
                attr_str += ", ..."
            preview = f"{type(value).__name__}({attr_str})"
        return {"type": type(value).__name__, "heap_ref": heap_id, "preview": preview,
                "display": preview, "attributes_count": len(attrs)}

    def _create_preview(self, obj) -> str:
        try:
            if isinstance(obj, (list, tuple)):
                if len(obj) == 0:
                    return "[]" if isinstance(obj, list) else "()"
                preview = [repr(x)[:15] for x in list(obj)[:MAX_ITER_PREVIEW]]
                suffix = ", ..." if len(obj) > MAX_ITER_PREVIEW else ""
                bracket = "[]" if isinstance(obj, list) else "()"
                return f'{bracket[0]}{", ".join(preview)}{suffix}{bracket[1]}'
            elif isinstance(obj, dict):
                if len(obj) == 0:
                    return "{}"
                items = list(obj.items())[:MAX_ITER_PREVIEW]
                preview = [f"{repr(k)[:10]}: {repr(v)[:10]}" for k, v in items]
                suffix = ", ..." if len(obj) > MAX_ITER_PREVIEW else ""
                return f'{{{", ".join(preview)}{suffix}}}'
            elif isinstance(obj, (set, frozenset)):
                if len(obj) == 0:
                    return "set()" if isinstance(obj, set) else "frozenset()"
                preview = [repr(x)[:15] for x in list(obj)[:MAX_ITER_PREVIEW]]
                suffix = ", ..." if len(obj) > MAX_ITER_PREVIEW else ""
                return f'{{{", ".join(preview)}{suffix}}}'
            return str(obj)[:50]
        except Exception:
            return f"<{type(obj).__name__}>"
