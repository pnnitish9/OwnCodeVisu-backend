#!/usr/bin/env python3
"""
Development entrypoint. For production use a proper WSGI server, e.g.:

    gunicorn -w 2 -b 0.0.0.0:5000 --timeout 60 wsgi:app

(`-w` worker count should stay low-ish since each request may itself spawn
a child process for sandboxed execution; see app/python_engine/sandbox.py
and app/cpp_engine/debugger.py.)
"""
from app import create_app
from app.config import config

app = create_app()

if __name__ == "__main__":
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG)
