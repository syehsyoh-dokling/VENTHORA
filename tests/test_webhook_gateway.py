"""The gateway a tunnel exposes passes Airwallex webhooks to the API and nothing else."""
import httpx
from starlette.testclient import TestClient

from venthora.webhook_gateway import MAX_BODY, create_app


def _gateway(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"received": True})
    upstream = httpx.AsyncClient(base_url="http://api", transport=httpx.MockTransport(handler))
    return TestClient(create_app(client=upstream))


def test_only_the_webhook_reaches_the_api():
    seen = []
    with _gateway(seen) as gw:
        for method, path in [("GET", "/"), ("GET", "/live"), ("GET", "/docs"), ("POST", "/v1/executions"),
                             ("GET", "/v1/org/users"), ("DELETE", "/v1/org/units/hq"), ("GET", "/webhooks/airwallex"),
                             ("POST", "/webhooks/airwallex/../../v1/plans"), ("POST", "/api/sandbox/deposits")]:
            assert gw.request(method, path).status_code == 404, (method, path)
        assert seen == []


def test_webhook_is_forwarded_raw_with_signature_headers_only():
    seen = []
    with _gateway(seen) as gw:
        body = b'{"id":"evt_1","name":"deposit.settled"}'
        r = gw.post("/webhooks/airwallex", content=body, headers={"x-timestamp": "1", "x-signature": "abc",
                                                                  "content-type": "application/json", "cookie": "s=1",
                                                                  "authorization": "Bearer x"})
        assert r.status_code == 200 and r.json() == {"received": True}
        (req,) = seen
        assert req.url.path == "/webhooks/airwallex" and req.content == body
        assert req.headers["x-signature"] == "abc" and req.headers["x-timestamp"] == "1"
        assert "cookie" not in req.headers and "authorization" not in req.headers


def test_inbound_email_is_forwarded_with_its_basic_auth_only():
    seen = []
    with _gateway(seen) as gw:
        r = gw.post("/webhooks/email", content=b'{"Subject":"x"}', headers={"content-type": "application/json",
                                                                          "authorization": "Basic abc", "cookie": "s=1",
                                                                          "x-signature": "nope"})
        assert r.status_code == 200
        (req,) = seen
        assert req.url.path == "/webhooks/email" and req.headers["authorization"] == "Basic abc"
        assert "cookie" not in req.headers and "x-signature" not in req.headers
        assert gw.get("/webhooks/email").status_code == 404


def test_large_body_refused_and_api_down_is_502():
    seen = []
    with _gateway(seen) as gw:
        assert gw.post("/webhooks/airwallex", content=b"x" * (MAX_BODY + 1)).status_code == 413
        assert seen == []

    def down(request):
        raise httpx.ConnectError("refused")
    gw = TestClient(create_app(client=httpx.AsyncClient(base_url="http://api", transport=httpx.MockTransport(down))))
    with gw:
        assert gw.post("/webhooks/airwallex", content=b"{}").status_code == 502
