"""Deterministic EU261 / UK261 compensation engine.

Everything here is computed, never narrated: distance comes from the bundled
airport table, the band from the regulation, the verdict from the arrival
delay. No model call, no network, no clock dependency except the dates the
caller passes in.

Regulation references (EU: Regulation (EC) No 261/2004; UK: the retained
version after exit, "UK261"):
  Art. 3(1)  scope: departure from an in-scope airport, or arrival on a
             Union/UK carrier
  Art. 5(1)(c) cancellation: no Art. 7 compensation at >= 14 days notice, or
             when re-routing lands within 2/3/4 h by band
  Art. 5(3)  extraordinary-circumstances defence
  Art. 6    delay -> right to care (meals, calls) at 2/3/4 h by band
  Art. 7(1) fixed compensation by great-circle distance band
  Art. 7(2) 50% reduction when re-routing exceeds the 2/3/4 h window
  Sturgeon (C-402/07, C-432/07): a 3+ h arrival delay earns Art. 7
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

# --- regulation constants -------------------------------------------------

# Art. 7(1): distance bands and the fixed amount per passenger.
EU_BANDS: tuple[tuple[float, int], ...] = (
    (1500.0, 250),
    (3500.0, 400),
    (float("inf"), 600),
)
UK_BANDS: tuple[tuple[float, int], ...] = (
    (1500.0, 220),
    (3500.0, 350),
    (float("inf"), 520),
)
BAND_LABELS = ("<= 1,500 km", "1,500-3,500 km", "> 3,500 km")

# Art. 6: right-to-care notice thresholds by band index.
CARE_HOURS = (2, 3, 4)

# Sturgeon: arrival delay that turns Art. 6 into Art. 7 compensation.
COMPENSATION_DELAY_HOURS = 3.0

# Art. 5(1)(c): cancellation notified this many days ahead is free.
CANCELLATION_NOTICE_DAYS = 14

# Art. 5(1)(c)(i)-(iii): re-routing that lands within these hours by band
# keeps the carrier out of Art. 7 even on a cancellation.
REROUTE_HOURS = (2, 3, 4)

EARTH_RADIUS_KM = 6371.0088


@dataclass(frozen=True)
class Leg:
    """One journey as the regulation sees it.

    ``via`` is intermediate stops; Art. 7(1) measures to the last destination
    at which the delay bites, so a connection adds its segments to the total.
    """

    origin: str
    destination: str
    via: tuple[str, ...] = ()

    @property
    def points(self) -> tuple[str, ...]:
        return (self.origin, *self.via, self.destination)


@dataclass
class Assessment:
    eligible: bool
    regime: str
    kind: str
    distance_km: float
    band: str
    amount: int
    currency: str
    per_passenger: int
    passengers: int
    delay_hours: float
    reason: str
    care: dict[str, Any] = field(default_factory=dict)
    citations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- airport table --------------------------------------------------------

_TABLE_CACHE: dict[str, dict[str, str]] | None = None


def _table_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "airports.tsv"


_COUNTRY_CACHE: dict[str, str] | None = None


def _countries_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "countries.tsv"


def load_countries() -> dict[str, str]:
    """ISO alpha-2 -> English country name. Bundled offline, 249 rows."""
    global _COUNTRY_CACHE
    if _COUNTRY_CACHE is not None:
        return _COUNTRY_CACHE
    rows: dict[str, str] = {}
    for line in _countries_path().read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip():
            rows[parts[0].strip().upper()] = parts[1].strip()
    _COUNTRY_CACHE = rows
    return rows


def load_airports() -> dict[str, dict[str, str]]:
    """IATA -> {name, city, country, country_name, lat, lon}. Bundled, ~7.9k rows."""
    global _TABLE_CACHE
    if _TABLE_CACHE is not None:
        return _TABLE_CACHE
    countries = load_countries()
    rows: dict[str, dict[str, str]] = {}
    for line in _table_path().read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        iata = parts[0].strip().upper()
        name, city, country, lat, lon = (p.strip() for p in parts[1:6])
        if not iata or iata in rows:
            continue
        try:
            float(lat), float(lon)
        except ValueError:
            continue
        cc = country.upper()
        rows[iata] = {
            "name": name, "city": city, "country": cc,
            "country_name": countries.get(cc, cc), "lat": lat, "lon": lon,
        }
    _TABLE_CACHE = rows
    return rows


def resolve_airport(code: str) -> dict[str, Any]:
    table = load_airports()
    key = (code or "").strip().upper()
    if key not in table:
        raise KeyError(
            f"unknown airport code {code!r}; pass an IATA code (e.g. BER, JFK), "
            "or search one first with lookup_airport"
        )
    return {"iata": key, **table[key]}


def lookup_airport(query: str, *, limit: int = 8) -> list[dict[str, Any]]:
    """Offline search by IATA, city or airport name."""
    q = (query or "").strip()
    if not q:
        return []
    qu, needle = q.upper(), q.lower()
    exact, starts, contains = [], [], []
    for iata, row in load_airports().items():
        name, city = row["name"].lower(), row["city"].lower()
        entry = {"iata": iata, **row}
        if iata == qu:
            exact.append(entry)
        elif iata.startswith(qu) or name.startswith(needle) or city.startswith(needle):
            starts.append(entry)
        elif needle in name or needle in city or needle in row["country"].lower():
            contains.append(entry)
    return (exact + starts + contains)[: max(1, min(limit, 50))]


def haversine_km(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Great-circle distance — the basis every regime uses for the band."""
    lat1, lon1 = math.radians(float(a["lat"])), math.radians(float(a["lon"]))
    lat2, lon2 = math.radians(float(b["lat"])), math.radians(float(b["lon"]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def leg_distance_km(leg: Leg) -> float:
    total = 0.0
    points = leg.points
    for a, b in zip(points, points[1:]):
        total += haversine_km(resolve_airport(a), resolve_airport(b))
    return round(total, 1)


def band_for(distance_km: float) -> int:
    if distance_km <= EU_BANDS[0][0]:
        return 0
    if distance_km <= EU_BANDS[1][0]:
        return 1
    return 2


# --- regime resolution ----------------------------------------------------

EU_COUNTRIES = frozenset(
    "AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE".split()
)
# EEA + Switzerland: EU261 applies by agreement (Art. 3(1) via the EEA Agreement).
EEA_EXTRA = frozenset({"IS", "LI", "NO", "CH"})
UK_COUNTRIES = frozenset({"GB"})


def resolve_regime(origin_country: str, destination_country: str, carrier_kind: str) -> str | None:
    """Which regime reaches this flight, per Art. 3(1).

    Accepts an ISO alpha-2 code or the English country name.
    """
    names = load_countries()
    by_name = {v.lower(): k for k, v in names.items()}

    def code(value: str) -> str:
        v = (value or "").strip()
        if not v:
            return ""
        if len(v) == 2:
            return v.upper()
        return by_name.get(v.lower(), "")

    o, d = code(origin_country), code(destination_country)
    k = (carrier_kind or "").strip().lower()
    eu_scope = o in EU_COUNTRIES or o in EEA_EXTRA
    uk_scope = o in UK_COUNTRIES
    if eu_scope:
        return "eu261"
    if uk_scope:
        return "uk261"
    if k == "union" and (d in EU_COUNTRIES or d in EEA_EXTRA):
        return "eu261"
    if k == "uk" and d in UK_COUNTRIES:
        return "uk261"
    return None


# --- the assessment -------------------------------------------------------

def _parse_via(via: Iterable[str] | str | None) -> tuple[str, ...]:
    if isinstance(via, str):
        raw = via.replace(" ", "").replace("->", ",").split(",")
    else:
        raw = list(via or ())
    return tuple(x.strip().upper() for x in raw if x and x.strip())


def _normalize(regime: str) -> str:
    r = (regime or "").strip().lower().replace("-", "").replace("_", "")
    if r in {"uk261", "uk", "gb261"}:
        return "uk261"
    return "eu261"


def assess(
    *,
    origin: str,
    destination: str,
    regime: str,
    kind: str,
    scheduled_arrival: datetime,
    actual_arrival: datetime | None = None,
    via: Iterable[str] | str | None = None,
    cancellation_notice_days: int | None = None,
    reroute_arrival_delta_hours: float | None = None,
    extraordinary: str | None = None,
    carrier_kind: str = "union",
    passengers: int = 1,
) -> Assessment:
    """One deterministic verdict. Inputs in, verdict out — no model call."""
    regime = _normalize(regime)
    kind = (kind or "delay").strip().lower()
    if kind not in {"delay", "cancellation", "denied_boarding"}:
        raise ValueError(f"kind must be delay, cancellation or denied_boarding (got {kind!r})")
    if kind == "denied_boarding":
        kind = "cancellation"  # Art. 4 gives the same Art. 7 compensation

    pax = max(1, int(passengers or 1))
    leg = Leg(origin.strip().upper(), destination.strip().upper(), _parse_via(via))
    distance = leg_distance_km(leg)
    band = band_for(distance)
    per_passenger = (EU_BANDS if regime == "eu261" else UK_BANDS)[band][1]
    currency = "EUR" if regime == "eu261" else "GBP"

    origin_row, dest_row = resolve_airport(leg.origin), resolve_airport(leg.destination)
    detected = resolve_regime(origin_row["country"], dest_row["country"], carrier_kind)
    warnings: list[str] = []
    if detected is None:
        warnings.append(
            f"neither endpoint is in EU/EEA/CH/UK scope and carrier_kind is {carrier_kind!r}: "
            "Art. 3(1) does not reach this flight, so this assessment is informational only"
        )
    elif detected != regime:
        warnings.append(
            f"you asked for {regime} but this route resolves to {detected}; "
            "the figures use the regime you asked for"
        )

    care = {
        "care_after_hours": CARE_HOURS[band],
        "note": f"Art. 6 right to care (meal, drinks, calls) starts at {CARE_HOURS[band]} h on this band",
    }

    def verdict(
        eligible: bool,
        reason: str,
        citations: list[str],
        *,
        per_pax: int,
        delay_hours: float,
        include_care: bool,
    ) -> Assessment:
        return Assessment(
            eligible=eligible,
            regime=regime,
            kind="cancellation" if kind == "cancellation" else "delay",
            distance_km=distance,
            band=BAND_LABELS[band],
            amount=per_pax * pax,
            currency=currency,
            per_passenger=per_pax,
            passengers=pax,
            delay_hours=round(delay_hours, 2),
            reason=reason,
            care=care if include_care else {},
            citations=citations,
            warnings=warnings,
        )

    if kind == "cancellation":
        if cancellation_notice_days is not None and cancellation_notice_days >= CANCELLATION_NOTICE_DAYS:
            return verdict(
                False,
                f"cancelled {cancellation_notice_days} days ahead: Art. 5(1)(c)(i) — at or beyond "
                f"{CANCELLATION_NOTICE_DAYS} days notice there is no Art. 7 compensation",
                ["Art. 5(1)(c)(i)"], per_pax=0, delay_hours=0.0, include_care=False,
            )
        if reroute_arrival_delta_hours is not None and reroute_arrival_delta_hours <= REROUTE_HOURS[band]:
            return verdict(
                False,
                f"re-routing arrived within {REROUTE_HOURS[band]} h of the original schedule: "
                "Art. 5(1)(c)(ii)/(iii) — no compensation",
                ["Art. 5(1)(c)(ii)"], per_pax=0, delay_hours=0.0, include_care=False,
            )
        if extraordinary:
            return verdict(
                False,
                f"the carrier claims an Art. 5(3) extraordinary circumstance ({extraordinary!r}): "
                "no compensation if that defence holds — contest it with the evidence",
                ["Art. 5(3)"], per_pax=0, delay_hours=0.0, include_care=True,
            )
        return verdict(
            True,
            "cancellation inside the notice window: Art. 5(1)(c) + Art. 7(1) fixed compensation of "
            f"{per_passenger} {currency} per passenger",
            ["Art. 5(1)(c)", "Art. 7(1)"], per_pax=per_passenger, delay_hours=0.0, include_care=True,
        )

    # delay / denied-boarding path
    if actual_arrival is None:
        raise ValueError("actual_arrival is required for a delay assessment")
    delay = max(0.0, (actual_arrival - scheduled_arrival).total_seconds() / 3600.0)

    if delay < COMPENSATION_DELAY_HOURS:
        return verdict(
            False,
            f"arrived {delay:.2f} h late: below the {COMPENSATION_DELAY_HOURS:.0f} h Sturgeon threshold, "
            "so Art. 7 compensation does not arise (the Art. 6 care rights may still)",
            ["Sturgeon C-402/07"], per_pax=0, delay_hours=delay, include_care=True,
        )
    if extraordinary:
        return verdict(
            False,
            f"delay of {delay:.2f} h, but the carrier claims an Art. 5(3) extraordinary circumstance "
            f"({extraordinary!r}): contest it with the evidence",
            ["Art. 5(3)", "Sturgeon C-402/07"], per_pax=0, delay_hours=delay, include_care=True,
        )
    return verdict(
        True,
        f"arrived {delay:.2f} h late: Sturgeon turns the delay into Art. 7(1) compensation of "
        f"{per_passenger} {currency} per passenger",
        ["Art. 6", "Art. 7(1)", "Sturgeon C-402/07"], per_pax=per_passenger, delay_hours=delay, include_care=True,
    )
