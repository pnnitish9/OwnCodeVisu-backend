# tracer/heap_manager.py
"""
Builds the "heap" object graph shown in the visualizer (lists, dicts, sets,
class instances, generators...).

Correctness note (fixed vs. the original implementation): a heap object's
*identity* -> heap id mapping is permanent (needed so reference arrows stay
stable across steps), but its *contents* are rebuilt every time the object
is re-visited. The original implementation cached an object's rendered
contents on first sight and reused them forever, so mutating a list/dict/
object attribute after that first appearance silently stopped showing up in
later steps - a serious bug for a *step-by-step* visualizer, whose entire
value proposition is showing state change over time. Cycles (an object that
(in)directly references itself) are handled with a per-call visiting set so
refreshing contents can never recurse forever.

Everything is also bounded (MAX_ELEMENTS, MAX_ATTRS, MAX_REPR_LEN,
MAX_HEAP_OBJECTS, MAX_DEPTH) so a single huge/cyclic/adversarial structure
can't blow up memory or the JSON response - it degrades to a truncated
view instead of failing the whole trace.
"""
import itertools
from typing import Dict, Any, Optional, Set
import inspect

MAX_ELEMENTS = 200          # per list/tuple/set/dict shown
MAX_ATTRS = 100              # per object/class
MAX_REPR_LEN = 300           # characters kept from any single repr()
MAX_HEAP_OBJECTS = 2000      # total distinct objects tracked in one trace
MAX_DEPTH = 25                # guard against pathological nesting


def _safe_repr(obj: Any, limit: int = MAX_REPR_LEN) -> str:
    try:
        text = repr(obj)
    except Exception as exc:  # a broken __repr__ must not crash the tracer
        return f"<unrepresentable {type(obj).__name__}: {exc}>"
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


class HeapManager:
    """Heap manager with support for complex objects, bounded so tracing
    never produces an unbounded response, and refreshed on every visit so
    mutable-object mutation is reflected at every step."""

    def __init__(self, max_objects: int = MAX_HEAP_OBJECTS):
        self.objects: Dict[int, int] = {}   # python id() -> stable heap id
        self.heap: Dict[int, dict] = {}      # heap id -> rendered contents
        self.id_counter = itertools.count(1)
        self.truncated = False
        self.max_objects = max_objects

    def add_object(self, obj: Any, object_name: str = "", _depth: int = 0,
                    _visiting: Optional[Set[int]] = None) -> int:
        """Add/refresh `obj` in the heap. Returns a stable integer heap id;
        identical Python objects (by identity) always map to the same heap
        id for the lifetime of this HeapManager, so the frontend can draw
        reference arrows consistently across steps."""
        if _visiting is None:
            _visiting = set()

        obj_id = id(obj)

        if obj_id in _visiting:
            # Cycle: this object is already being (re)built further up the
            # current recursion chain. Don't recurse again - just point at
            # its (already-registered) heap id.
            return self.objects.get(obj_id) or self._register_new_id(obj_id)

        already_known = obj_id in self.objects
        if not already_known and len(self.heap) >= self.max_objects:
            self.truncated = True
            heap_id = self._register_new_id(obj_id)
            self.heap[heap_id] = {
                "type": type(obj).__name__,
                "label": "(heap object limit reached)",
                "truncated": True,
            }
            return heap_id

        heap_id = self.objects[obj_id] if already_known else self._register_new_id(obj_id)

        if _depth >= MAX_DEPTH:
            self.heap[heap_id] = {
                "type": type(obj).__name__,
                "label": "(max nesting depth reached)",
                "value": _safe_repr(obj),
                "truncated": True,
            }
            return heap_id

        _visiting.add(obj_id)
        try:
            self._render(obj, heap_id, object_name, _depth, _visiting)
        except Exception as exc:
            # A user-defined __len__/__iter__/__eq__/etc. could itself
            # raise; make sure that never takes the whole trace down.
            self.heap[heap_id] = {"type": type(obj).__name__, "label": f"(error inspecting object: {exc})"}
        finally:
            _visiting.discard(obj_id)

        return heap_id

    def _register_new_id(self, obj_id: int) -> int:
        heap_id = next(self.id_counter)
        self.objects[obj_id] = heap_id
        return heap_id

    def _render(self, obj, heap_id, name, depth, visiting):
        if isinstance(obj, (list, tuple)):
            self._store_sequence(obj, heap_id, name, depth, visiting)
        elif isinstance(obj, dict):
            self._store_dict(obj, heap_id, name, depth, visiting)
        elif isinstance(obj, (set, frozenset)):
            self._store_set(obj, heap_id, name, depth, visiting)
        elif inspect.isclass(obj):
            self._store_class(obj, heap_id, name, depth, visiting)
        elif inspect.isgenerator(obj):
            self._store_generator(obj, heap_id, name)
        elif hasattr(obj, "__dict__"):
            self._store_object(obj, heap_id, name, depth, visiting)
        else:
            self._store_primitive(obj, heap_id)

    def _detect_dimensions(self, obj):
        """
        Detect if a list/tuple is a regular 2D or 3D array (rectangular grid).
        Returns a list [rows, cols] or [depth, rows, cols] if rectangular, else None.
        """
        if not isinstance(obj, (list, tuple)) or len(obj) == 0:
            return None
        
        # Check if all elements are sequences (potential 2D array)
        if not all(isinstance(item, (list, tuple)) for item in obj):
            return None
        
        # Check for uniform row lengths (2D rectangular array)
        first_row_len = len(obj[0]) if isinstance(obj[0], (list, tuple)) else None
        if first_row_len is None:
            return None
        
        if all(isinstance(row, (list, tuple)) and len(row) == first_row_len for row in obj):
            # We have a 2D array
            # Check if it's actually 3D (all rows contain uniform lists)
            if first_row_len > 0 and all(isinstance(obj[0][j], (list, tuple)) for j in range(first_row_len)):
                # Potential 3D array - check if all elements at [i][j] are lists of same length
                first_depth_len = len(obj[0][0]) if isinstance(obj[0][0], (list, tuple)) else None
                if first_depth_len is not None:
                    is_3d = True
                    for row in obj:
                        if not isinstance(row, (list, tuple)) or len(row) != first_row_len:
                            is_3d = False
                            break
                        for col in row:
                            if not isinstance(col, (list, tuple)) or len(col) != first_depth_len:
                                is_3d = False
                                break
                        if not is_3d:
                            break
                    
                    if is_3d:
                        # Check that innermost elements are primitives (not more nesting)
                        all_primitives = True
                        for layer in obj:
                            for row in layer:
                                for item in row:
                                    if isinstance(item, (list, tuple)):
                                        all_primitives = False
                                        break
                                if not all_primitives:
                                    break
                            if not all_primitives:
                                break
                        
                        if all_primitives:
                            return [len(obj), first_row_len, first_depth_len]  # [depth, rows, cols]
            
            # It's a 2D array (not 3D)
            return [len(obj), first_row_len]  # [rows, cols]
        
        return None

    def _store_sequence(self, obj, heap_id, name, depth, visiting):
        n = len(obj)
        entry = {"type": "list" if isinstance(obj, list) else "tuple",
                  "label": f"{name or type(obj).__name__}[{n}]", "elements": [],
                  "mutable": isinstance(obj, list), "length": n}
        
        # Detect multidimensional array structure
        dimensions = self._detect_dimensions(obj)
        if dimensions:
            entry["dimensions"] = dimensions
            entry["is_matrix"] = True
        
        shown = 0
        for i, item in enumerate(obj):
            if shown >= MAX_ELEMENTS:
                entry["elements"].append({"index": i, "type": "truncated", "value": f"... {n - shown} more item(s) not shown"})
                break
            if self._is_heap_type(item):
                ref_id = self.add_object(item, f"{name}[{i}]" if name else f"[{i}]", depth + 1, visiting)
                entry["elements"].append({"index": i, "type": "ref", "heap_id": ref_id})
            else:
                entry["elements"].append({"index": i, "type": "value", "value": _safe_repr(item), "value_type": type(item).__name__})
            shown += 1
        self.heap[heap_id] = entry

    def _store_dict(self, obj, heap_id, name, depth, visiting):
        n = len(obj)
        entry = {"type": "dict", "label": f'{name or "dict"}[{n}]', "entries": [], "length": n}
        shown = 0
        for key, value in obj.items():
            if shown >= MAX_ELEMENTS:
                entry["entries"].append({
                    "key": f"... {n - shown} more entr{'y' if n - shown == 1 else 'ies'} not shown",
                    "key_type": "", "value": {"type": "value", "value": ""},
                })
                break
            item = {"key": _safe_repr(key), "key_type": type(key).__name__}
            if self._is_heap_type(value):
                ref_id = self.add_object(value, f"{name}[{key}]" if name else str(key), depth + 1, visiting)
                item["value"] = {"type": "ref", "heap_id": ref_id}
            else:
                item["value"] = {"type": "value", "value": _safe_repr(value), "value_type": type(value).__name__}
            entry["entries"].append(item)
            shown += 1
        self.heap[heap_id] = entry

    def _store_set(self, obj, heap_id, name, depth, visiting):
        n = len(obj)
        label_type = "frozenset" if isinstance(obj, frozenset) else "set"
        entry = {"type": label_type, "label": f'{name or label_type}[{n}]', "elements": [], "length": n}
        shown = 0
        for item in obj:
            if shown >= MAX_ELEMENTS:
                entry["elements"].append({"type": "truncated", "value": f"... {n - shown} more item(s)"})
                break
            if self._is_heap_type(item):
                ref_id = self.add_object(item, _depth=depth + 1, _visiting=visiting)
                entry["elements"].append({"type": "ref", "heap_id": ref_id})
            else:
                entry["elements"].append({"type": "value", "value": _safe_repr(item)})
            shown += 1
        self.heap[heap_id] = entry

    def _store_class(self, cls, heap_id, name, depth, visiting):
        entry = {
            "type": "class", "label": f"class {cls.__name__}", "methods": [], "attributes": [],
            "bases": [base.__name__ for base in cls.__bases__ if base is not object],
        }
        for method_name, method in list(inspect.getmembers(cls, predicate=inspect.isfunction))[:MAX_ATTRS]:
            if not method_name.startswith("_"):
                try:
                    sig = str(inspect.signature(method))
                except (ValueError, TypeError):
                    sig = "(...)"
                entry["methods"].append({"name": method_name, "signature": sig})

        for attr_name, attr_value in list(cls.__dict__.items())[:MAX_ATTRS]:
            if attr_name.startswith("_") or inspect.isfunction(attr_value):
                continue
            if self._is_heap_type(attr_value):
                ref_id = self.add_object(attr_value, _depth=depth + 1, _visiting=visiting)
                entry["attributes"].append({"name": attr_name, "type": "ref", "heap_id": ref_id})
            else:
                entry["attributes"].append({"name": attr_name, "type": "value", "value": _safe_repr(attr_value)})
        self.heap[heap_id] = entry

    def _store_generator(self, gen, heap_id, name):
        entry = {"type": "generator", "label": f'generator {name or ""}', "state": "suspended", "frame_info": {}}
        try:
            if gen.gi_frame:
                entry["frame_info"] = {
                    "line": gen.gi_frame.f_lineno,
                    "code": gen.gi_code.co_name,
                    "locals": {k: _safe_repr(v) for k, v in list(gen.gi_frame.f_locals.items())[:MAX_ATTRS] if not k.startswith("__")},
                }
            elif gen.gi_code:
                entry["state"] = "exhausted"
        except Exception:
            pass
        self.heap[heap_id] = entry

    def _store_object(self, obj, heap_id, name, depth, visiting):
        entry = {"type": "object", "label": f"{name or type(obj).__name__} instance",
                  "class": type(obj).__name__, "attributes": [], "methods": []}
        items = list(obj.__dict__.items())
        for attr_name, attr_value in items[:MAX_ATTRS]:
            if self._is_heap_type(attr_value):
                ref_id = self.add_object(attr_value, f"{name}.{attr_name}" if name else attr_name, depth + 1, visiting)
                entry["attributes"].append({"name": attr_name, "type": "ref", "heap_id": ref_id})
            else:
                entry["attributes"].append({
                    "name": attr_name, "type": "value",
                    "value": _safe_repr(attr_value), "value_type": type(attr_value).__name__,
                })
        if len(items) > MAX_ATTRS:
            entry["attributes"].append({"name": "...", "type": "truncated", "value": f"{len(items) - MAX_ATTRS} more attribute(s)"})

        shown = 0
        for method_name in dir(obj):
            if shown >= MAX_ATTRS:
                break
            if method_name.startswith("_"):
                continue
            try:
                method = getattr(obj, method_name)
                if callable(method) and not isinstance(method, type):
                    try:
                        sig = str(inspect.signature(method))
                    except (ValueError, TypeError):
                        sig = "(...)"
                    entry["methods"].append({"name": method_name, "signature": sig})
                    shown += 1
            except Exception:
                continue
        self.heap[heap_id] = entry

    def _store_primitive(self, obj, heap_id):
        self.heap[heap_id] = {"type": type(obj).__name__, "value": _safe_repr(obj)}

    def _is_heap_type(self, obj: Any) -> bool:
        return isinstance(obj, (list, dict, set, frozenset, tuple)) or (
            hasattr(obj, "__dict__") and not isinstance(obj, (str, int, float, bool, type(None), type))
        )

    def get_all_objects(self) -> Dict:
        return dict(self.heap)

    def clear(self):
        self.objects.clear()
        self.heap.clear()
        self.id_counter = itertools.count(1)
        self.truncated = False
