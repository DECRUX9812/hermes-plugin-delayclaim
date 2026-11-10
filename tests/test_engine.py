"""Engine smoke tests — deterministic, offline, no network."""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delayclaim.engine import (  # noqa: E402
    Leg,
    assess,
    leg_distance_km,
    lookup_airport,
    resolve_airport,
    resolve_regime,
)


def test_airport_lookup():
    hits = [a["iata"] for a in lookup_airport("Berlin", limit=3)]
    assert "BER" in hits, hits
    assert resolve_airport("ber")["country_name"] == "Germany"


def test_distance_bands():
    assert leg_distance_km(Leg("BER", "JFK")) > 3500
    via = leg_distance_km(Leg("BER", "JFK", ("FRA",)))
    assert via > leg_distance_km(Leg("BER", "JFK"))
    assert leg_distance_km(Leg("BER", "MUC")) < 1500


def test_delay_short_haul_not_eligible():
    s = datetime(2026, 10, 3, 9, 0)
    a = datetime(2026, 10, 3, 10, 30)  # 1.5 h
    r = assess(origin="BER", destination="MUC", regime="eu261", kind="delay",
               scheduled_arrival=s, actual_arrival=a)
    assert not r.eligible
    assert r.amount == 0
    assert r.care["care_after_hours"] == 2


def test_delay_long_haul_eligible():
    s = datetime(2026, 10, 3, 14, 0)
    a = datetime(2026, 10, 3, 19, 20)  # 5.33 h
    r = assess(origin="BER", destination="JFK", regime="eu261", kind="delay",
               scheduled_arrival=s, actual_arrival=a, passengers=2)
    assert r.eligible
    assert r.per_passenger == 600
    assert r.amount == 1200
    assert r.currency == "EUR"
    assert r.band == "> 3,500 km"


def test_uk_regime_amounts():
    s = datetime(2026, 10, 3, 9, 0)
    a = datetime(2026, 10, 3, 13, 30)
    r = assess(origin="LHR", destination="JFK", regime="uk261", kind="delay",
               scheduled_arrival=s, actual_arrival=a)
    assert r.eligible and r.per_passenger == 520 and r.currency == "GBP"


def test_cancellation_notice_window():
    s = datetime(2026, 10, 3, 9, 0)
    r = assess(origin="BER", destination="JFK", regime="eu261", kind="cancellation",
               scheduled_arrival=s, cancellation_notice_days=20)
    assert not r.eligible and r.amount == 0
    r2 = assess(origin="BER", destination="JFK", regime="eu261", kind="cancellation",
                scheduled_arrival=s, cancellation_notice_days=3)
    assert r2.eligible and r2.per_passenger == 600


def test_extraordinary_circumstances_blocks():
    s = datetime(2026, 10, 3, 14, 0)
    a = datetime(2026, 10, 3, 20, 0)
    r = assess(origin="BER", destination="JFK", regime="eu261", kind="delay",
               scheduled_arrival=s, actual_arrival=a, extraordinary="volcanic ash")
    assert not r.eligible and r.amount == 0
    assert "Art. 5(3)" in r.citations


def test_regime_detection():
    assert resolve_regime("Germany", "United States", "union") == "eu261"
    assert resolve_regime("United Kingdom", "United States", "uk") == "uk261"
    assert resolve_regime("United States", "Germany", "union") == "eu261"
    assert resolve_regime("United States", "Germany", "nonunion") is None


def test_every_airport_has_coordinates():
    from delayclaim.engine import load_airports
    table = load_airports()
    assert len(table) > 5000
    for iata, row in list(table.items())[:200]:
        float(row["lat"]); float(row["lon"])
