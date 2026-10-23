from fastapi.testclient import TestClient

from reliability.main_api import create_app
from reliability.platform.ids import uuid7


def test_healthz_and_headers():
    client = TestClient(create_app())
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert resp.headers["X-Request-Id"].startswith("req_")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


def test_unknown_route_uses_error_envelope():
    client = TestClient(create_app())
    resp = client.get("/v1/nope")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["request_id"]


def test_uuid7_is_version_7_and_time_ordered():
    a, b = uuid7(), uuid7()
    assert a.version == 7 and b.version == 7
    assert a.variant == "specified in RFC 4122"
    assert str(a)[:8] <= str(b)[:8]
