# ruff: noqa: E501
"""System Setting -> Hackathon evidence: every organiser item points at a screen that exists in the menu and, where it
has a live probe, at a documented GET endpoint that answers."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

INDEX = Path(__file__).resolve().parents[1] / "src" / "venthora" / "dashboard" / "index.html"
SLOW = {"/v1/agent-plan", "/v1/cards", "/v1/card-authorization", "/v1/visa/vic", "/v1/metal", "/v1/mcp", "/v1/webhooks/status", "/v1/cash-placement"}  # these reach a bank or a network


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def test_every_item_points_at_a_real_screen_and_a_documented_probe(client):
    from venthora import apidocs

    d = client.get("/v1/evidence").json()
    items = [i for g in d["groups"] for i in g["items"]]
    assert d["counts"]["total"] == len(items) and d["counts"]["done"] > d["counts"]["blocked"]
    html = INDEX.read_text(encoding="utf-8")
    navs = set(re.findall(r'data-nav="([\w-]+)"', html))
    for i in items:
        assert i["status"] in ("done", "partial", "blocked", "na"), i
        if i["status"] == "blocked":
            assert i.get("waiting"), f"{i['ref']} is blocked: name the sponsor it waits on"
        if i.get("screen", "").startswith("#/"):
            assert i["screen"][2:] in navs, f"{i['ref']}: no menu entry {i['screen']}"
        if i.get("probe"):
            assert ("GET", i["probe"]) in apidocs.DOCS, f"{i['ref']}: probe {i['probe']} is not a documented GET"
    for probe in {i["probe"] for i in items if i.get("probe")} - SLOW:
        assert client.get(probe).status_code == 200, probe
