"""
Test that standard library imports work correctly in the sandbox.
Run with: pytest tests/test_imports.py -v
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


def test_collections_import():
    code = """
from collections import deque
q = deque([1, 2, 3])
q.append(4)
print(len(q))
"""
    result = trace(code)
    assert "4" in result["output"]
    assert not any(s.get("exception_info") for s in result["steps"])


def test_collections_queue_import():
    code = """
from collections import deque as queue
q = queue()
q.append(1)
q.append(2)
print(len(q))
"""
    result = trace(code)
    assert "2" in result["output"]
    assert not any(s.get("exception_info") for s in result["steps"])


def test_math_import():
    code = """
import math
result = math.sqrt(16)
print(result)
"""
    result = trace(code)
    assert "4.0" in result["output"]


def test_random_import():
    code = """
import random
random.seed(42)
x = random.randint(1, 100)
print(f"Random: {x}")
"""
    result = trace(code)
    assert "Random:" in result["output"]


def test_itertools_import():
    code = """
from itertools import combinations
nums = [1, 2, 3]
combos = list(combinations(nums, 2))
print(len(combos))
"""
    result = trace(code)
    assert "3" in result["output"]


def test_heapq_import():
    code = """
import heapq
heap = [3, 1, 4, 1, 5]
heapq.heapify(heap)
print(heapq.heappop(heap))
"""
    result = trace(code)
    assert "1" in result["output"]


def test_datetime_import():
    code = """
from datetime import datetime
now = datetime(2024, 1, 1)
print(now.year)
"""
    result = trace(code)
    assert "2024" in result["output"]


def test_json_import():
    code = """
import json
data = {"name": "Alice", "age": 30}
s = json.dumps(data)
print("age" in s)
"""
    result = trace(code)
    assert "True" in result["output"]


def test_re_import():
    code = """
import re
pattern = r'\\d+'
text = "abc123def"
match = re.search(pattern, text)
print(match.group())
"""
    result = trace(code)
    assert "123" in result["output"]


def test_disallowed_import_fails():
    code = """
import os
print(os.getcwd())
"""
    result = trace(code)
    exc = last_exception(result)
    assert exc is not None
    assert exc["type"] == "ImportError"
    assert "not allowed" in exc["message"]


def test_disallowed_subprocess_import():
    code = """
import subprocess
subprocess.run(['ls'])
"""
    result = trace(code)
    exc = last_exception(result)
    assert exc is not None
    assert exc["type"] == "ImportError"


def test_disallowed_sys_import():
    code = """
import sys
print(sys.version)
"""
    result = trace(code)
    exc = last_exception(result)
    assert exc is not None
    assert exc["type"] == "ImportError"


def test_multiple_imports():
    code = """
import math
from collections import deque
import random

result = math.sqrt(16)
q = deque([1, 2])
random.seed(42)
r = random.randint(1, 10)

print(f"{result} {len(q)} {r}")
"""
    result = trace(code)
    assert "4.0" in result["output"]
    assert "2" in result["output"]


def test_import_alias():
    code = """
import collections as col
d = col.defaultdict(int)
d['a'] += 1
print(d['a'])
"""
    result = trace(code)
    assert "1" in result["output"]


if __name__ == "__main__":
    # Quick manual test
    print("Testing collections import...")
    test_collections_import()
    print("✓ collections works")
    
    print("Testing disallowed import...")
    test_disallowed_import_fails()
    print("✓ disallowed imports blocked")
    
    print("\nAll manual tests passed!")
