"""Shared fixtures.

The ``live_app`` fixture runs the real Flask app in a background thread on an
ephemeral port. Phase 2 tests need a real browser talking to a real server over
HTTP -- an in-process test client cannot exercise iframes, the accessibility tree,
or Playwright at all.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from werkzeug.serving import make_server

from target_app.app import create_app


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def client() -> Iterator[Any]:
    """Flask test client, for assertions that do not need a browser."""
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture(scope="session")
def live_app() -> Iterator[str]:
    """Serve the target app on a real port for the duration of the session.

    Yields the base URL. Shut down deterministically rather than left to a daemon
    thread, so a hung server fails the suite loudly instead of silently.
    """
    app = create_app()
    port = _free_port()
    server = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, name="target-app")
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
