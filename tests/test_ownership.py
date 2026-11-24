"""Road ownership (NSRN) and municipal boundaries (NS open data)."""
import pytest

from roadsense.core import ownership


@pytest.mark.parametrize("lat, lon, name", [
    (44.6455, -63.5735, "Halifax Regional Municipality"),
    (44.5410, -64.2390, "Municipality of the District of Chester"),
    (44.3780, -64.5190, "Town of Bridgewater"),
    (44.0390, -64.7160, "Region of Queens Municipality"),
])
def test_municipality_at_route_points(lat, lon, name):
    assert ownership.municipality_at(lat, lon)["name"] == name


def test_municipality_outside_nova_scotia():
    assert ownership.municipality_at(43.65, -79.38) is None  # Toronto
    assert ownership.municipality_at(44.30, -63.20) is None  # Atlantic Ocean off Halifax


def test_boundaries_file_has_49():
    assert len(ownership.municipalities()) == 49


# ---------------- NSRN road owner ----------------
import httpx  # noqa: E402

from roadsense.common.db import session  # noqa: E402

LAT, LON = 44.5410, -64.2390  # Duke St, Chester


def _row(owner, street, metres_north, desc=None):
    lat = LAT + metres_north / 111_320
    return {"the_geom": {"type": "MultiLineString", "coordinates": [[[LON - 0.001, lat], [LON + 0.001, lat]]]},
            "street": street, "owner": owner, "owner_desc": desc or owner}


class _Fake:
    """Stands in for httpx.get: replays one answer per call and counts the calls."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    def __call__(self, url, **kw):
        self.calls += 1
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return httpx.Response(200, json=a, request=httpx.Request("GET", url))


@pytest.fixture
def online(monkeypatch, db):
    from roadsense.common.config import settings
    object.__setattr__(settings, "offline_geo", False)  # settings is frozen
    monkeypatch.setattr(ownership, "_down_until", 0.0)

    def install(*answers):
        fake = _Fake(*answers)
        monkeypatch.setattr(ownership.httpx, "get", fake)
        return fake
    try:
        yield install
    finally:
        object.__setattr__(settings, "offline_geo", True)


def test_owner_picks_nearest_segment(online):
    online([_row("M", "Other St", -30, "Municipal"), _row("TIR", "Duke St", 10, "TIR")])
    with session() as conn:
        o = ownership.road_owner(conn, LAT, LON)
    assert o["owner"] == "TIR" and o["street"] == "Duke St" and 9 <= o["distance_m"] <= 11 and o["tie"] is None


def test_owner_tie_at_intersection(online):
    online([_row("TIR", "Highway 3", 3), _row("M", "King St", -6, "Municipal")])
    with session() as conn:
        o = ownership.road_owner(conn, LAT, LON)
    assert o["owner"] == "TIR" and o["tie"]["owner"] == "M" and o["tie"]["street"] == "King St"


def test_owner_same_owner_nearby_is_not_a_tie(online):
    online([_row("M", "King St", 3), _row("M", "Queen St", -6)])
    with session() as conn:
        assert ownership.road_owner(conn, LAT, LON)["tie"] is None


def test_owner_miss_is_cached(online):
    fake = online([])
    with session() as conn:
        first = ownership.road_owner(conn, LAT, LON)
        second = ownership.road_owner(conn, LAT, LON)
    assert first["owner"] is None and second["owner"] is None and fake.calls == 1


def test_owner_error_is_not_cached(online, monkeypatch):
    fake = online(httpx.ConnectError("down"))
    with session() as conn:
        assert ownership.road_owner(conn, LAT, LON) is None
        assert ownership.road_owner(conn, LAT, LON) is None  # breaker open: no second request
        assert conn.execute("SELECT COUNT(*) FROM geocache WHERE key LIKE 'nsrn:%'").fetchone()[0] == 0
    assert fake.calls == 1
    monkeypatch.setattr(ownership, "_down_until", 0.0)
    online({"error": True, "message": "query.compiler.malformed"})  # Socrata error body with HTTP 200
    with session() as conn:
        assert ownership.road_owner(conn, LAT, LON) is None
        assert conn.execute("SELECT COUNT(*) FROM geocache WHERE key LIKE 'nsrn:%'").fetchone()[0] == 0


def test_owner_offline_makes_no_request(db, monkeypatch):
    monkeypatch.setattr(ownership.httpx, "get", _Fake())  # any call would pop from an empty list and fail
    with session() as conn:
        assert ownership.road_owner(conn, LAT, LON) is None
