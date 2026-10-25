"""The dashboard pages load cleanly: every page is served, links the favicon, and no probe needs a 404."""
from fastapi.testclient import TestClient


def test_pages_favicon_and_latest_run(monkeypatch, tmp_path):
    monkeypatch.setenv("VENTHORA_EXECUTION_DB", str(tmp_path / "e.db"))
    monkeypatch.setenv("VENTHORA_INTAKE_DB", str(tmp_path / "i.db"))
    monkeypatch.setenv("VENTHORA_AUDIT_DB", str(tmp_path / "a.db"))
    from venthora.api import app
    with TestClient(app) as client:
        for path in ("/", "/live", "/flow"):
            r = client.get(path)
            assert r.status_code == 200, path
            assert 'rel="icon" href="/favicon.svg"' in r.text, path
        for path in ("/favicon.svg", "/favicon.ico"):
            r = client.get(path)
            assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml"), path
        sessions = getattr(app.state, "live", None)
        if sessions:
            sessions.clear()
        assert client.get("/api/live/latest").json() == {"sid": None}
