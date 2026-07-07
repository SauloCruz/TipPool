"""SquareClient request-shape tests over a mock HTTP transport: multi-location
coverage (one venue, two Square locations, one pool) and pagination."""

import json

import httpx
import pytest

from app.config import Settings
from app.square import SquareClient


def make_client(handler, locations=("LOC_BAR", "LOC_ANNEX")):
    transport = httpx.MockTransport(handler)
    http = httpx.Client(base_url="https://connect.squareupsandbox.com",
                        transport=transport)
    return SquareClient("tok", list(locations), env="sandbox", http=http)


class TestMultiLocation:
    def test_payments_looped_per_location_and_merged(self):
        seen = []

        def handler(request):
            loc = request.url.params["location_id"]
            seen.append(loc)
            return httpx.Response(200, json={
                "payments": [{"id": f"P_{loc}", "status": "COMPLETED"}]})

        client = make_client(handler)
        payments = client.list_payments("b", "e")
        assert seen == ["LOC_BAR", "LOC_ANNEX"]
        assert [p["id"] for p in payments] == ["P_LOC_BAR", "P_LOC_ANNEX"]

    def test_orders_sends_all_location_ids_in_one_search(self):
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"orders": [{"id": "O1"}]})

        client = make_client(handler)
        orders = client.search_orders("b", "e")
        assert len(bodies) == 1  # native multi-location: single search
        assert bodies[0]["location_ids"] == ["LOC_BAR", "LOC_ANNEX"]
        assert len(orders) == 1

    def test_timecards_and_team_send_all_location_ids(self):
        bodies = []

        def handler(request):
            bodies.append((request.url.path, json.loads(request.content)))
            key = "timecards" if "labor" in request.url.path else "team_members"
            return httpx.Response(200, json={key: []})

        client = make_client(handler)
        client.search_timecards("b", "e")
        client.search_team_members()
        tc_body = dict(bodies)["/v2/labor/timecards/search"]
        tm_body = dict(bodies)["/v2/team-members/search"]
        assert tc_body["query"]["filter"]["location_ids"] == ["LOC_BAR", "LOC_ANNEX"]
        assert tm_body["query"]["filter"]["location_ids"] == ["LOC_BAR", "LOC_ANNEX"]

    def test_payments_pagination_within_each_location(self):
        calls = []

        def handler(request):
            loc = request.url.params["location_id"]
            cursor = request.url.params.get("cursor")
            calls.append((loc, cursor))
            if loc == "LOC_BAR" and cursor is None:
                return httpx.Response(200, json={
                    "payments": [{"id": "P1"}], "cursor": "page2"})
            return httpx.Response(200, json={"payments": [{"id": f"P_{loc}_{cursor}"}]})

        client = make_client(handler)
        payments = client.list_payments("b", "e")
        assert calls == [("LOC_BAR", None), ("LOC_BAR", "page2"), ("LOC_ANNEX", None)]
        assert len(payments) == 3

    def test_single_location_string_still_accepted(self):
        c = SquareClient("tok", "LOC_ONLY", env="sandbox",
                         http=httpx.Client(transport=httpx.MockTransport(
                             lambda r: httpx.Response(200, json={"payments": []}))))
        assert c.location_ids == ["LOC_ONLY"]

    def test_empty_locations_rejected(self):
        with pytest.raises(ValueError):
            SquareClient("tok", [], env="sandbox")


class TestConfigParsing:
    def test_comma_separated_env(self, monkeypatch):
        monkeypatch.setenv("SQUARE_LOCATION_ID", "LOC_BAR, LOC_ANNEX")
        monkeypatch.setenv("SQUARE_ACCESS_TOKEN", "tok")
        s = Settings(env_file="/nonexistent")
        assert s.square_location_ids == ["LOC_BAR", "LOC_ANNEX"]
        assert s.square_configured

    def test_single_location_env(self, monkeypatch):
        monkeypatch.setenv("SQUARE_LOCATION_ID", "LOC_ONLY")
        monkeypatch.setenv("SQUARE_ACCESS_TOKEN", "tok")
        s = Settings(env_file="/nonexistent")
        assert s.square_location_ids == ["LOC_ONLY"]

    def test_unset_means_unconfigured(self, monkeypatch):
        monkeypatch.delenv("SQUARE_LOCATION_ID", raising=False)
        monkeypatch.setenv("SQUARE_ACCESS_TOKEN", "tok")
        s = Settings(env_file="/nonexistent")
        assert s.square_location_ids == []
        assert not s.square_configured
