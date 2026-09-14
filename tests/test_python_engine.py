"""
Run with: pytest tests/test_python_engine.py -v
(These import python_engine directly and don't require flask_cors, so they
run even in environments where the full Flask app can't be imported.)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from python_engine.tracer import AdvancedPythonTracer  # noqa: E402


def trace(code, user_input="", **kwargs):
    tracer = AdvancedPythonTracer(**kwargs)
    return tracer.trace(code, user_input)


def last_exception(result):
    for step in reversed(result["steps"]):
        if step.get("exception_info"):
            return step["exception_info"]
    return None


def last_line_step(result):
    """The trace ends with a synthetic 'return' step for the module frame
    (whose stack_locals is just {'__return__': ...}); tests that want to
    inspect ordinary variables should look at the last *line* step."""
    for step in reversed(result["steps"]):
        if step["event"] == "line":
            return step
    raise AssertionError("no 'line' step found in trace")


def test_basic_execution_and_output():
    result = trace('print("hi")\nx = 1 + 2\nprint(x)')
    assert result["output"] == "hi\n3\n"
    assert result["total_steps"] > 0
    assert not result["truncated"]


def test_variables_tracked_with_types():
    result = trace("x = 5\ny = 'hello'\nz = None\n")
    last = last_line_step(result)
    assert last["stack_locals"]["x"]["type"] == "int"
    assert last["stack_locals"]["y"]["type"] == "str"


def test_heap_object_refreshes_on_mutation():
    result = trace("nums = [1, 2]\nnums.append(3)\nnums.append(4)\ndone = True\n")
    last = last_line_step(result)
    heap_id = last["stack_locals"]["nums"]["heap_ref"]
    heap_obj = last["heap_state"][str(heap_id)] if str(heap_id) in last["heap_state"] else last["heap_state"][heap_id]
    assert heap_obj["length"] == 4


def test_object_attribute_mutation_reflected():
    code = """
class P:
    def __init__(self, x):
        self.x = x
p = P(1)
p.x = 999
done = True
"""
    result = trace(code)
    last = last_line_step(result)
    heap_id = last["stack_locals"]["p"]["heap_ref"]
    heap_obj = last["heap_state"][str(heap_id)] if str(heap_id) in last["heap_state"] else last["heap_state"][heap_id]
    x_attr = next(a for a in heap_obj["attributes"] if a["name"] == "x")
    assert x_attr["value"] == "999"


def test_self_referencing_list_does_not_hang():
    result = trace("a = []\na.append(a)\n", timeout_seconds=3)
    assert not result["truncated"]
    assert result["output"] == ""


def test_zero_division_classified():
    result = trace("x = 1 / 0\n")
    exc = last_exception(result)
    assert exc["type"] == "ZeroDivisionError"
    assert exc["hint"]


def test_index_error_classified():
    result = trace("lst = [1, 2]\nlst[10]\n")
    exc = last_exception(result)
    assert exc["type"] == "IndexError"


def test_key_error_classified():
    result = trace("d = {}\nd['missing']\n")
    exc = last_exception(result)
    assert exc["type"] == "KeyError"


def test_recursion_error_is_resource_limit_category():
    result = trace("def f(n):\n    return f(n + 1)\nf(0)\n", max_steps=200000)
    exc = last_exception(result)
    assert exc["type"] == "RecursionError"
    assert exc["category"] == "resource_limit"


def test_syntax_error_propagates_uncaught():
    tracer = AdvancedPythonTracer()
    try:
        tracer.trace("def f(:\n    pass\n")
        assert False, "expected SyntaxError"
    except SyntaxError as exc:
        assert exc.lineno == 1


def test_step_limit_truncates_gracefully():
    result = trace("x = 0\nwhile True:\n    x += 1\n", max_steps=50, timeout_seconds=5)
    assert result["truncated"]
    assert result["truncation_reason"] == "step_limit"
    assert result["total_steps"] <= 60  # +1 for the synthetic truncation step


def test_timeout_truncates_gracefully():
    result = trace("x = 0\nwhile True:\n    x += 1\n", max_steps=10_000_000, timeout_seconds=0.3)
    assert result["truncated"]
    assert result["truncation_reason"] == "timeout"


def test_output_limit_truncates_gracefully():
    result = trace("while True:\n    print('x' * 50)\n", max_output_chars=200, timeout_seconds=5)
    assert result["truncated"]
    assert result["truncation_reason"] == "output_limit"
    assert len(result["output"]) <= 200


def test_blocked_import_does_not_crash_tracer():
    result = trace("import os\n")
    exc = last_exception(result)
    assert exc is not None  # some ImportError/NameError-flavored failure, not a hard crash


def test_open_is_unavailable():
    result = trace("open('/etc/passwd')\n")
    exc = last_exception(result)
    assert exc is not None


def test_broken_repr_does_not_crash_tracer():
    code = """
class Bad:
    def __repr__(self):
        raise ValueError("boom")
b = Bad()
print("survived")
"""
    result = trace(code)
    assert result["output"] == "survived\n"


def test_generator_tracked():
    code = """
def gen():
    yield 1
    yield 2
g = gen()
for x in g:
    pass
"""
    result = trace(code)
    assert len(result["generators"]) >= 1


def test_scripted_input_reads_from_user_input():
    result = trace("name = input('Name: ')\nprint(f'hi {name}')\n", user_input="Ada")
    assert "hi Ada" in result["output"]


def test_input_exhausted_raises_clean_eof():
    result = trace("a = input()\nb = input()\n", user_input="only-one-line")
    exc = last_exception(result)
    assert exc["type"] == "EOFError"


def test_decorator_and_class_sample_end_to_end():
    code = """
def logger(func):
    def wrapper(*args, **kwargs):
        print(f"Calling {func.__name__}")
        result = func(*args, **kwargs)
        return result
    return wrapper

@logger
def power(base, exp):
    return base ** exp

result = power(2, 5)
print(f"Final: {result}")
"""
    result = trace(code)
    assert "Final: 32" in result["output"]
    assert not any(s.get("exception_info") for s in result["steps"])
