# app/__init__.py
import logging
import sys

from flask import Flask
from flask_cors import CORS

from .config import config
from .errors import register_error_handlers
from .routes import bp as api_bp


def _configure_logging(debug: bool):
    level = logging.DEBUG if debug else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    ))
    logger = logging.getLogger("codeviz")
    logger.setLevel(level)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False

    # Quiet werkzeug's default per-request access log noise at INFO; keep
    # warnings/errors so real problems still surface.
    logging.getLogger("werkzeug").setLevel(logging.WARNING if not debug else logging.INFO)


def create_app() -> Flask:
    _configure_logging(config.DEBUG)
    logger = logging.getLogger("codeviz")

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH
    app.config["JSON_SORT_KEYS"] = False

    cors_origins = config.CORS_ORIGINS
    CORS(app, resources={r"/*": {"origins": cors_origins}})
    if cors_origins == ["*"]:
        logger.warning(
            "CORS_ORIGINS is '*' - fine for local development, but set it to "
            "your actual frontend origin(s) before deploying publicly."
        )

    register_error_handlers(app)
    app.register_blueprint(api_bp)

    logger.info("CodeViz backend starting (env=%s, debug=%s)", config.ENV, config.DEBUG)
    return app
