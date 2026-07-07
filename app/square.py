"""Thin Square REST client. Deliberately logic-free: it fetches and paginates,
nothing else — all interpretation lives in square_extract.py where it can be
unit-tested without a network. Token stays server-side (CLAUDE.md §3).

Uses SearchTimecards (Labor API) — the Shift endpoints are deprecated;
pinned Square-Version 2025-05-21 per spec."""

from __future__ import annotations

import httpx

SQUARE_VERSION = "2025-05-21"
BASE_URLS = {
    "production": "https://connect.squareup.com",
    "sandbox": "https://connect.squareupsandbox.com",
}


class SquareError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Square API {status}: {message}")
        self.status = status


class SquareClient:
    """One venue may span multiple Square locations; every per-day pull
    covers all of them so the tip pool is venue-wide. Orders/Timecards/Team
    searches take location_ids natively; the Payments list endpoint is
    single-location, so payments loop per location and concatenate."""

    def __init__(self, token: str, location_ids: list[str] | str,
                 env: str = "production", http: httpx.Client | None = None):
        if isinstance(location_ids, str):
            location_ids = [s.strip() for s in location_ids.split(",") if s.strip()]
        if not location_ids:
            raise ValueError("at least one Square location id is required")
        self.location_ids = location_ids
        self._http = http or httpx.Client(
            base_url=BASE_URLS[env],
            headers={
                "Authorization": f"Bearer {token}",
                "Square-Version": SQUARE_VERSION,
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    def _request(self, method: str, path: str, *, params=None, body=None) -> dict:
        try:
            r = self._http.request(method, path, params=params, json=body)
        except httpx.HTTPError as exc:
            raise SquareError(0, f"network error: {exc}") from exc
        if r.status_code >= 400:
            try:
                detail = "; ".join(e.get("detail", e.get("code", "?"))
                                   for e in r.json().get("errors", []))
            except Exception:
                detail = r.text[:200]
            raise SquareError(r.status_code, detail or "request failed")
        return r.json()

    def _paged_get(self, path: str, params: dict, list_key: str) -> list[dict]:
        out, cursor = [], None
        while True:
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            data = self._request("GET", path, params=p)
            out.extend(data.get(list_key, []))
            cursor = data.get("cursor")
            if not cursor:
                return out

    def _paged_post(self, path: str, body: dict, list_key: str) -> list[dict]:
        out, cursor = [], None
        while True:
            b = dict(body)
            if cursor:
                b["cursor"] = cursor
            data = self._request("POST", path, body=b)
            out.extend(data.get(list_key, []))
            cursor = data.get("cursor")
            if not cursor:
                return out

    # ---- per-day pulls ----

    def list_payments(self, begin_iso: str, end_iso: str) -> list[dict]:
        out = []
        for loc in self.location_ids:
            out.extend(self._paged_get("/v2/payments", {
                "begin_time": begin_iso, "end_time": end_iso,
                "location_id": loc, "limit": 100,
            }, "payments"))
        return out

    def search_orders(self, begin_iso: str, end_iso: str) -> list[dict]:
        return self._paged_post("/v2/orders/search", {
            "location_ids": self.location_ids,
            "query": {
                "filter": {
                    "state_filter": {"states": ["COMPLETED"]},
                    "date_time_filter": {"closed_at": {
                        "start_at": begin_iso, "end_at": end_iso}},
                },
                "sort": {"sort_field": "CLOSED_AT"},
            },
            "limit": 100,
        }, "orders")

    def search_timecards(self, begin_iso: str, end_iso: str) -> list[dict]:
        return self._paged_post("/v2/labor/timecards/search", {
            "query": {
                "filter": {
                    "location_ids": self.location_ids,
                    "start": {"start_at": begin_iso, "end_at": end_iso},
                },
                "sort": {"field": "START_AT", "order": "ASC"},
            },
            "limit": 200,
        }, "timecards")

    # ---- mapping syncs ----

    def batch_retrieve_catalog(self, object_ids: list[str]) -> dict:
        """Returns {'objects': [...], 'related_objects': [...]} for variation
        ids, including their items and categories."""
        objects, related = [], []
        for i in range(0, len(object_ids), 100):
            data = self._request("POST", "/v2/catalog/batch-retrieve", body={
                "object_ids": object_ids[i:i + 100],
                "include_related_objects": True,
            })
            objects.extend(data.get("objects", []))
            related.extend(data.get("related_objects", []))
        return {"objects": objects, "related_objects": related}

    def list_categories(self) -> list[dict]:
        return self._paged_get("/v2/catalog/list", {"types": "CATEGORY"}, "objects")

    def search_team_members(self) -> list[dict]:
        return self._paged_post("/v2/team-members/search", {
            "query": {"filter": {"location_ids": self.location_ids,
                                 "status": "ACTIVE"}},
            "limit": 100,
        }, "team_members")
