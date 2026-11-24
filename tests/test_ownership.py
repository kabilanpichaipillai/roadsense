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
