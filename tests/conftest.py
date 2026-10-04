import threading

import pytest
from werkzeug.serving import make_server

from bag.bankapp.app import create_app

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


class StubGuard:
    """A guard that gives every action the same ruling and remembers what it was asked.

    For tests of the loops that do not care about the real rules. The terminal actions
    done / ask_human are always allowed, as they are by the real guard.
    """

    def __init__(self, decision=None, reason=""):
        from bag.safety import Decision

        self.decision = decision or Decision.ALLOW
        self.reason, self.seen = reason, []

    def check_url(self, url):
        from bag.safety import Decision, Verdict

        return Verdict(Decision.ALLOW)

    def authorize(self, action, context, *, run="", step=None):
        from bag.safety import Decision, Verdict

        self.seen.append((action, context, run, step))
        if getattr(action, "action", None) in ("done", "ask_human"):
            return Verdict(Decision.ALLOW)
        return Verdict(self.decision, self.reason)
