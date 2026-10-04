import threading

import pytest
from werkzeug.serving import make_server

from lba.bankapp.app import create_app

BANK_USER = "teller-test"
BANK_PASSWORD = "not-a-real-password"


@pytest.fixture(scope="session")
def bank_url():
    """A real bank app running on a free port, for tests that need a real browser."""
    # A developer's own fault switches must not leak in.
    mp = pytest.MonkeyPatch()
    for name in ("BANK_POPUP", "BANK_SLOW", "BANK_PERM", "SESSION_TTL"):
        mp.delenv(name, raising=False)
    app = create_app({"BANK_USER": BANK_USER, "BANK_PASSWORD": BANK_PASSWORD})
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    mp.undo()
