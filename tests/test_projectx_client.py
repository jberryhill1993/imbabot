"""Unit tests for ProjectXClient request shaping + response parsing.

These use a stub `requests.Session` so we verify the exact URLs, JSON bodies and
auth headers the client sends — without any network — covering the one layer the
FakeClient-based selftest can't (the real HTTP client).

Run:  python -m pytest tests/  (or: python tests/test_projectx_client.py)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imbabot.models import OrderSide, OrderType  # noqa: E402
from imbabot.projectx import ProjectXClient, ProjectXError  # noqa: E402


class StubResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self):
        return self._payload


class StubSession:
    """Records calls and returns queued responses keyed by URL suffix."""

    def __init__(self, routes):
        self.routes = routes          # {path_suffix: payload}
        self.calls = []               # list of (url, json, headers)

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        for suffix, payload in self.routes.items():
            if url.endswith(suffix):
                return StubResponse(payload)
        return StubResponse({"success": False, "errorCode": 404, "errorMessage": "no route"}, 404)


def make_client(routes):
    sess = StubSession(routes)
    return ProjectXClient(session=sess), sess


def test_auth_sets_bearer_header():
    client, sess = make_client({"/api/Auth/loginKey": {"token": "JWT123", "success": True, "errorCode": 0}})
    token = client.authenticate("alice", "secret-key")
    assert token == "JWT123"
    url, body, headers = sess.calls[-1]
    assert url.endswith("/api/Auth/loginKey")
    assert body == {"userName": "alice", "apiKey": "secret-key"}
    # next authed call must carry the bearer token
    client.routes = None
    sess.routes["/api/Account/search"] = {"accounts": [], "success": True, "errorCode": 0}
    client.search_accounts()
    _, _, headers2 = sess.calls[-1]
    assert headers2["Authorization"] == "Bearer JWT123"
    print("PASS test_auth_sets_bearer_header")


def test_place_order_body_includes_brackets():
    routes = {
        "/api/Auth/loginKey": {"token": "T", "success": True, "errorCode": 0},
        "/api/Order/place": {"orderId": 555, "success": True, "errorCode": 0},
    }
    client, sess = make_client(routes)
    client.authenticate("u", "k")
    res = client.place_order(
        account_id=7, contract_id="CON.X", order_type=OrderType.STOP,
        side=OrderSide.BUY, size=2, stop_price=21012.0,
        custom_tag="t-L", stop_loss_ticks=48, take_profit_ticks=56,
    )
    assert res.success and res.order_id == 555
    _, body, _ = sess.calls[-1]
    assert body["type"] == 4 and body["side"] == 0 and body["size"] == 2
    assert body["stopPrice"] == 21012.0 and body["customTag"] == "t-L"
    assert body["stopLossBracket"] == {"ticks": 48, "type": 4}
    assert body["takeProfitBracket"] == {"ticks": 56, "type": 1}
    print("PASS test_place_order_body_includes_brackets")


def test_success_false_raises():
    routes = {
        "/api/Auth/loginKey": {"token": "T", "success": True, "errorCode": 0},
        "/api/Order/cancel": {"success": False, "errorCode": 12, "errorMessage": "no such order"},
    }
    client, sess = make_client(routes)
    client.authenticate("u", "k")
    try:
        client.cancel_order(1, 999)
        raise AssertionError("expected ProjectXError")
    except ProjectXError as exc:
        assert exc.error_code == 12
    print("PASS test_success_false_raises")


def test_resolve_contract_prefers_active_match():
    routes = {
        "/api/Auth/loginKey": {"token": "T", "success": True, "errorCode": 0},
        "/api/Contract/search": {
            "success": True, "errorCode": 0,
            "contracts": [
                {"id": "CON.OLD", "name": "MNQU5", "activeContract": False,
                 "tickSize": 0.25, "tickValue": 0.5, "symbolId": "F.US.MNQ"},
                {"id": "CON.NEW", "name": "MNQZ5", "activeContract": True,
                 "tickSize": 0.25, "tickValue": 0.5, "symbolId": "F.US.MNQ"},
            ],
        },
    }
    client, sess = make_client(routes)
    client.authenticate("u", "k")
    c = client.resolve_contract("MNQ")
    assert c.id == "CON.NEW" and c.active
    print("PASS test_resolve_contract_prefers_active_match")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} client tests passed")


if __name__ == "__main__":
    _run_all()
