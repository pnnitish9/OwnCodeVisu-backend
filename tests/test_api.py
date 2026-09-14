"""
Run with: pytest tests/test_api.py -v
Requires flask + flask-cors installed (see requirements.txt).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402
from app import create_app  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def post_json(client, path, payload):
    return client.post(path, data=json.dumps(payload), content_type="application/json")


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.get_json()["status"] == "ok"


def test_languages(client):
    r = client.get("/api/languages")
    assert r.status_code == 200
    body = r.get_json()
    assert body["success"] is True
    assert body["data"]["python"]["available"] is True


def test_trace_python_success(client):
    r = post_json(client, "/api/python/trace", {"code": "print('hi')"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["success"] is True
    assert body["data"]["output"] == "hi\n"


def test_trace_python_legacy_alias(client):
    r = post_json(client, "/trace", {"code": "print(1)"})
    assert r.status_code == 200
    assert r.get_json()["success"] is True


def test_trace_missing_code_field(client):
    r = post_json(client, "/api/python/trace", {})
    assert r.status_code == 400
    body = r.get_json()
    assert body["success"] is False
    assert body["error"]["category"] == "validation"


def test_trace_invalid_json(client):
    r = client.post("/api/python/trace", data="not json", content_type="application/json")
    assert r.status_code == 400
    assert r.get_json()["error"]["category"] == "validation"


def test_trace_wrong_type_for_code(client):
    r = post_json(client, "/api/python/trace", {"code": 123})
    assert r.status_code == 400


def test_trace_syntax_error(client):
    r = post_json(client, "/api/python/trace", {"code": "def f(:\n"})
    assert r.status_code == 422
    body = r.get_json()
    assert body["error"]["category"] == "syntax"
    assert body["error"]["line"] == 1


def test_trace_code_too_long(client):
    r = post_json(client, "/api/python/trace", {"code": "x=1\n" * 100000})
    assert r.status_code == 400


def test_trace_runtime_exception(client):
    r = post_json(client, "/api/python/trace", {"code": "1/0"})
    assert r.status_code == 200  # a well-formed trace that happens to end in an error
    body = r.get_json()
    assert body["success"] is True
    last = body["data"]["steps"][-1]
    assert last["exception_info"]["type"] == "ZeroDivisionError"


def test_404_shape(client):
    r = client.get("/api/does/not/exist")
    assert r.status_code == 404
    body = r.get_json()
    assert body["success"] is False
    assert body["error"]["type"] == "NotFound"


def test_405_shape(client):
    r = client.get("/api/python/trace")  # POST-only endpoint
    assert r.status_code == 405


def test_cpp_trace_endpoint_reachable(client):
    r = post_json(client, "/api/cpp/trace", {"code": "int main(){return 0;}"})
    assert r.status_code in (200, 422, 503)  # depends on whether g++/gdb are installed
