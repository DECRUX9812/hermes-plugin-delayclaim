"""Agent tools for Delayclaim: assess, draft, and track one claim per passenger.

The tools are thin. Every number comes from ``delayclaim.engine``, which is
deterministic and offline; nothing here calls a model, and nothing sends
anything to an airline. A draft letter is written to the plugin's own data
directory and handed back for the person to send.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .engine import (
    Assessment,
    Leg,
    assess,
    leg_distance_km,
    load_airports,
    lookup_airport,
    resolve_airport,
    resolve_regime,
)

TOOLSET = "delayclaim"

# Recommended follow-up cadence, not a statutory deadline. The regulation sets
# none for a claim; the two real clocks a person needs are the enforcement
# route (after a reasonable wait) and the court limitation period.
NO_REPLY_ESCALATE_DAYS = 56
COURT_LIMITATION_YEARS = 2  # Montreal Convention Art. 35(1)

_DISCLAIMER = (
    "This is a computed assessment under Regulation (EC) No 261/2004 (or its UK "
    "retained version) and general guidance, not legal advice. The airline decides; "
    "a national enforcement body or a court decides disputes."
)


# --- storage --------------------------------------------------------------

def _db_path(home: Path) -> Path:
    d = home / "plugin-data" / "delayclaim"
    d.mkdir(parents=True, exist_ok=True)
    return d / "cases.db"


def _connect(home: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(home), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference TEXT NOT NULL UNIQUE,
            passenger TEXT NOT NULL DEFAULT '',
            carrier TEXT NOT NULL DEFAULT '',
            flight_number TEXT NOT NULL DEFAULT '',
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            via TEXT NOT NULL DEFAULT '',
            flight_date TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL,
            regime TEXT NOT NULL,
            distance_km REAL NOT NULL,
            band TEXT NOT NULL,
            per_passenger INTEGER NOT NULL,
            currency TEXT NOT NULL,
            amount INTEGER NOT NULL,
            eligible INTEGER NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            sent_on TEXT,
            their_reply_on TEXT,
            outcome TEXT,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS cases_status ON cases(status)")
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


# --- letter ---------------------------------------------------------------

def _money(amount: int, currency: str) -> str:
    symbol = "€" if currency == "EUR" else "£"
    return f"{symbol}{amount:,}"


def render_letter(
    *,
    assessment: Assessment,
    passenger: str,
    carrier: str,
    flight_number: str,
    flight_date: str,
    origin: str,
    destination: str,
    booking_reference: str = "",
    extra_expenses: str = "",
    their_address: str = "",
) -> str:
    """A demand letter that states only what the engine computed."""
    reg = "Regulation (EC) No 261/2004" if assessment.regime == "eu261" else "the UK retained version of Regulation (EC) No 261/2004 (UK261)"
    today = datetime.now(timezone.utc).strftime("%d %B %Y")
    lines = [
        f"{passenger}" if passenger else "[your name]",
        "",
        f"{today}",
        "",
        their_address.strip() or f"{carrier} — Customer Relations",
        "",
        f"Re: claim for compensation under {reg}",
        f"Flight {flight_number or '[number]'} · {origin} to {destination} · {flight_date or '[date]'}"
        + (f" · booking {booking_reference}" if booking_reference else ""),
        "",
        "Dear Sir or Madam,",
        "",
        f"I am claiming compensation under Article 7 of {reg} for the disruption to the "
        f"flight above. The great-circle distance of the journey is "
        f"{assessment.distance_km:,.0f} km ({assessment.band}), and I reached my final "
        f"destination {assessment.delay_hours:g} hours after the scheduled arrival time."
        if assessment.kind == "delay"
        else f"I am claiming compensation under Articles 5 and 7 of {reg} for the "
        f"cancellation of the flight above. The great-circle distance of the journey is "
        f"{assessment.distance_km:,.0f} km ({assessment.band}).",
        "",
    ]
    if assessment.kind == "delay":
        lines += [
            "In Joined Cases C-402/07 and C-432/07 (Sturgeon and Bock), the Court of Justice "
            "held that a passenger who reaches the final destination three hours or more after "
            "the scheduled arrival time is entitled to the compensation in Article 7. I therefore "
            f"claim {_money(assessment.per_passenger, assessment.currency)} per passenger, "
            f"{assessment.passengers} passenger(s) in total "
            f"({assessment.amount:,} {assessment.currency}).",
        ]
    else:
        lines += [
            "Article 5(1)(c) provides that compensation is not owed only where the cancellation "
            "was notified at least fourteen days before departure, or where re-routing brought me "
            "to my final destination within the applicable window. Neither exception applies here. "
            f"I therefore claim {_money(assessment.per_passenger, assessment.currency)} per "
            f"passenger, {assessment.passengers} passenger(s) in total "
            f"({assessment.amount:,} {assessment.currency}).",
        ]
    lines += [
        "",
        "Article 5(3) allows a carrier to refuse payment only where the disruption was caused by "
        "extraordinary circumstances that could not have been avoided even if all reasonable "
        "measures had been taken. If you rely on that provision, please state the specific "
        "circumstance and the measures you took; a technical fault or a crew rostering problem "
        "does not meet that test.",
        "",
        "In addition, under Article 9 I was entitled to care (meals, refreshments and "
        f"communication) from {assessment.care.get('care_after_hours', '-')} hours of delay. "
        "Please reimburse any reasonable expenses I incurred that were not provided."
        if assessment.care
        else "",
    ]
    if extra_expenses.strip():
        lines += ["", "Expenses claimed:", extra_expenses.strip()]
    lines += [
        "",
        "Please pay the amount above to the account I will confirm, or reply setting out the "
        "basis on which you refuse. If I do not receive a substantive reply I will refer the "
        "matter to the national enforcement body and, if necessary, to a court.",
        "",
        "Please treat this letter as a formal claim. I look forward to your reply within 8 weeks.",
        "",
        "Yours faithfully,",
        "",
        passenger or "[your name]",
        "",
        "---",
        "",
        "Assessment basis (computed, not narrated):",
        f"  regime: {assessment.regime}",
        f"  kind: {assessment.kind}",
        f"  distance: {assessment.distance_km:,.0f} km ({assessment.band})",
        f"  delay: {assessment.delay_hours:g} h",
        f"  per passenger: {assessment.per_passenger} {assessment.currency}",
        f"  passengers: {assessment.passengers}",
        f"  total: {assessment.amount} {assessment.currency}",
        f"  citations: {', '.join(assessment.citations) or 'none'}",
        "",
        _DISCLAIMER,
    ]
    return "\n".join(line for line in lines if line is not None)


# --- tool schemas ---------------------------------------------------------

ASSESS_SCHEMA = {
    "name": "delayclaim_assess",
    "description": (
        "Compute the fixed EU261/UK261 compensation for a disrupted flight, deterministically. "
        "Use this whenever the user asks what an airline owes them for a delayed, cancelled or "
        "overbooked flight. It resolves the airports from a bundled offline table, measures the "
        "great-circle distance (including connections), applies the Article 7 distance band, the "
        "3-hour arrival threshold, the 14-day cancellation notice window and the extraordinary-"
        "circumstances defence, then returns the amount per passenger, the total for the party, "
        "the right-to-care threshold, and the articles it relied on. It answers what the "
        "regulation provides; it does not file anything."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "origin": {"type": "string", "description": "Departure airport IATA code, e.g. BER"},
            "destination": {"type": "string", "description": "Final destination airport IATA code, e.g. JFK"},
            "via": {"type": "string", "description": "Comma-separated connection IATA codes, if the itinerary had any, e.g. FRA"},
            "regime": {"type": "string", "enum": ["eu261", "uk261", "auto"], "description": "Which regime to apply; 'auto' resolves it from the route and the carrier"},
            "kind": {"type": "string", "enum": ["delay", "cancellation", "denied_boarding"], "description": "What happened"},
            "scheduled_arrival": {"type": "string", "description": "Scheduled arrival at the final destination, ISO 8601 local time, e.g. 2026-10-03T14:00"},
            "actual_arrival": {"type": "string", "description": "Actual arrival at the final destination (delay/denied boarding only), ISO 8601 local time"},
            "cancellation_notice_days": {"type": "integer", "description": "How many days before departure the cancellation was notified (cancellation only)"},
            "reroute_arrival_delta_hours": {"type": "number", "description": "If re-routed, how many hours later than the original schedule the replacement arrived"},
            "extraordinary": {"type": "string", "description": "The circumstance the airline is invoking as its Article 5(3) defence, e.g. 'volcanic ash'"},
            "carrier_kind": {"type": "string", "enum": ["union", "uk", "nonunion"], "description": "union = EU/EEA carrier, uk = UK carrier, nonunion = neither"},
            "passengers": {"type": "integer", "description": "Number of passengers claiming (default 1)"},
        },
        "required": ["origin", "destination", "scheduled_arrival"],
    },
}

LETTER_SCHEMA = {
    "name": "delayclaim_letter",
    "description": (
        "Draft the formal compensation claim letter for an assessed flight, with the regulation "
        "and case citations in the text. Returns the letter as Markdown and writes it to the "
        "plugin's data directory. It never sends: the person reviews and sends it themselves, so "
        "nothing leaves the machine without their action. Call delayclaim_assess first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "origin": {"type": "string", "description": "Departure airport IATA code"},
            "destination": {"type": "string", "description": "Final destination IATA code"},
            "scheduled_arrival": {"type": "string", "description": "Scheduled arrival, ISO 8601 local time"},
            "actual_arrival": {"type": "string", "description": "Actual arrival, ISO 8601 local time"},
            "kind": {"type": "string", "enum": ["delay", "cancellation", "denied_boarding"]},
            "regime": {"type": "string", "enum": ["eu261", "uk261", "auto"]},
            "via": {"type": "string", "description": "Connection IATA codes, comma-separated"},
            "carrier": {"type": "string", "description": "Airline name"},
            "flight_number": {"type": "string", "description": "Flight number, e.g. LH400"},
            "flight_date": {"type": "string", "description": "Date of the flight as written on the booking"},
            "passenger": {"type": "string", "description": "Passenger name as on the booking"},
            "booking_reference": {"type": "string", "description": "PNR / booking reference"},
            "passengers": {"type": "integer", "description": "Number of passengers claiming"},
            "extra_expenses": {"type": "string", "description": "Itemised expenses (meals, hotel, calls) to claim under Article 9"},
            "their_address": {"type": "string", "description": "Where to address the letter; defaults to the airline's customer relations"},
            "cancellation_notice_days": {"type": "integer", "description": "Days of notice before departure (cancellation only)"},
            "extraordinary": {"type": "string", "description": "The carrier's Article 5(3) defence, if raised"},
        },
        "required": ["origin", "destination", "scheduled_arrival", "carrier"],
    },
}

CASE_OPEN_SCHEMA = {
    "name": "delayclaim_case_open",
    "description": (
        "Open a tracked case for one disrupted flight, storing the assessment, the airline, the "
        "passenger and the letter in the plugin's local database so it survives restarts and "
        "profile switches. Returns the case reference and the follow-up dates: 56 days for a "
        "substantive reply before referring to the national enforcement body, and the two-year "
        "court limitation period. Nothing is sent."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "origin": {"type": "string"}, "destination": {"type": "string"},
            "scheduled_arrival": {"type": "string"}, "actual_arrival": {"type": "string"},
            "kind": {"type": "string", "enum": ["delay", "cancellation", "denied_boarding"]},
            "regime": {"type": "string", "enum": ["eu261", "uk261", "auto"]},
            "via": {"type": "string"},
            "carrier": {"type": "string"}, "flight_number": {"type": "string"},
            "flight_date": {"type": "string"}, "passenger": {"type": "string"},
            "passengers": {"type": "integer"},
            "cancellation_notice_days": {"type": "integer"}, "extraordinary": {"type": "string"},
            "notes": {"type": "string"},
        },
        "required": ["origin", "destination", "scheduled_arrival", "carrier"],
    },
}

CASE_UPDATE_SCHEMA = {
    "name": "delayclaim_case_update",
    "description": (
        "Record what happened on a tracked case: mark it sent, note the airline's reply, or record "
        "the outcome (paid, refused, escalated, time-barred). Returns the case with its next action "
        "date. Use this after the person tells you they sent the letter or heard back."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reference": {"type": "string", "description": "Case reference returned by delayclaim_case_open"},
            "status": {"type": "string", "enum": ["draft", "sent", "awaiting_reply", "replied", "escalated", "paid", "refused", "closed"]},
            "sent_on": {"type": "string", "description": "YYYY-MM-DD the letter was sent"},
            "their_reply_on": {"type": "string", "description": "YYYY-MM-DD of the airline's reply"},
            "outcome": {"type": "string", "description": "What the airline did, in one line"},
            "notes": {"type": "string", "description": "Anything worth keeping with the case"},
        },
        "required": ["reference"],
    },
}

CASE_LIST_SCHEMA = {
    "name": "delayclaim_case_list",
    "description": (
        "List tracked cases with total claimed, total recovered and the next action date for each. "
        "Use this when the user asks what is still outstanding or where their claims stand."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Only show cases in this status"},
            "limit": {"type": "integer", "description": "How many cases (default 20)"},
        },
        "required": [],
    },
}

AIRPORT_SCHEMA = {
    "name": "delayclaim_airport",
    "description": (
        "Look up airports offline by IATA code, city or airport name, to resolve the exact codes "
        "a claim needs. Use this when the user names a city rather than an airport code."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "IATA code, city or airport name, e.g. 'Berlin' or 'BER'"},
            "limit": {"type": "integer", "description": "How many results (default 8)"},
        },
        "required": ["query"],
    },
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "delayclaim_assess": ASSESS_SCHEMA,
    "delayclaim_letter": LETTER_SCHEMA,
    "delayclaim_case_open": CASE_OPEN_SCHEMA,
    "delayclaim_case_update": CASE_UPDATE_SCHEMA,
    "delayclaim_case_list": CASE_LIST_SCHEMA,
    "delayclaim_airport": AIRPORT_SCHEMA,
}


# --- helpers --------------------------------------------------------------

def _parse_dt(value: str) -> datetime:
    text = (value or "").strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d %b %Y %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    raise ValueError(f"could not read {value!r} as a date-time; pass ISO 8601 like 2026-10-03T14:00")


def _build_assessment(args: dict[str, Any]) -> Assessment:
    regime = str(args.get("regime") or "auto").strip().lower()
    origin = str(args.get("origin") or "").strip().upper()
    destination = str(args.get("destination") or "").strip().upper()
    carrier_kind = str(args.get("carrier_kind") or "union").strip().lower()
    if regime == "auto":
        o_country = resolve_airport(origin)["country"]
        d_country = resolve_airport(destination)["country"]
        detected = resolve_regime(o_country, d_country, carrier_kind)
        regime = detected or "eu261"
    scheduled = _parse_dt(str(args.get("scheduled_arrival") or ""))
    actual_raw = args.get("actual_arrival")
    actual = _parse_dt(str(actual_raw)) if actual_raw else None
    kind = str(args.get("kind") or "delay").strip().lower()
    notice = args.get("cancellation_notice_days")
    reroute = args.get("reroute_arrival_delta_hours")
    return assess(
        origin=origin,
        destination=destination,
        via=args.get("via"),
        regime=regime,
        kind=kind,
        scheduled_arrival=scheduled,
        actual_arrival=actual,
        cancellation_notice_days=int(notice) if notice is not None else None,
        reroute_arrival_delta_hours=float(reroute) if reroute is not None else None,
        extraordinary=(str(args.get("extraordinary") or "").strip() or None),
        carrier_kind=carrier_kind,
        passengers=int(args.get("passengers") or 1),
    )


def _next_action(case: sqlite3.Row) -> str:
    """The date and reason the person should do something next."""
    status = case["status"]
    if status == "sent" and case["sent_on"]:
        try:
            sent = datetime.strptime(case["sent_on"], "%Y-%m-%d")
        except ValueError:
            return "unknown (sent date unreadable)"
        escalate = (sent + timedelta(days=NO_REPLY_ESCALATE_DAYS)).strftime("%Y-%m-%d")
        return f"{escalate} — no substantive reply by then, refer to the national enforcement body"
    if status == "draft" and case["flight_date"]:
        try:
            flight = datetime.strptime(case["flight_date"], "%Y-%m-%d")
            bar = (flight.replace(year=flight.year + COURT_LIMITATION_YEARS)).strftime("%Y-%m-%d")
            return f"{bar} — court limitation period (Montreal Convention Art. 35), 2 years from the flight"
        except (ValueError, TypeError):
            pass
    if status in {"paying", "paid"}:
        return "none — recovered"
    if status == "refused":
        return "escalate: national enforcement body, then a court"
    return "send the letter, then record it with delayclaim_case_update"


def _row_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "reference": row["reference"],
        "passenger": row["passenger"],
        "carrier": row["carrier"],
        "flight": f"{row['origin']}-{row['destination']}" + (f" via {row['via']}" if row["via"] else ""),
        "flight_number": row["flight_number"],
        "date": row["flight_date"],
        "kind": row["kind"],
        "regime": row["regime"],
        "amount": row["amount"],
        "currency": row["currency"],
        "status": row["status"],
        "eligible": bool(row["eligible"]),
        "next_action": _next_action(row),
    }


# --- handlers -------------------------------------------------------------

def make_handlers(plugin_dir: Path, home: Callable[[], Path]) -> dict[str, Callable[..., Any]]:
    """Build handlers bound to this plugin's own directory and data home."""

    def assess_handler(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            a = _build_assessment(args)
        except KeyError as exc:
            return _json({"ok": False, "error": str(exc).strip('"')})
        except Exception as exc:  # noqa: BLE001 - tool handlers must not raise
            return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return _json({"ok": True, **a.as_dict(), "disclaimer": _DISCLAIMER})

    def letter_handler(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            a = _build_assessment(args)
        except Exception as exc:  # noqa: BLE001
            return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        if not a.eligible:
            return _json({
                "ok": True,
                "eligible": False,
                "letter": None,
                "reason": a.reason,
                "note": "No compensation is owed on this assessment, so there is nothing to demand. "
                        "A refusal letter is not drafted.",
                "assessment": a.as_dict(),
            })
        text = render_letter(
            assessment=a,
            passenger=str(args.get("passenger") or "").strip(),
            carrier=str(args.get("carrier") or "").strip(),
            flight_number=str(args.get("flight_number") or "").strip(),
            flight_date=str(args.get("flight_date") or "").strip(),
            origin=str(args.get("origin") or "").strip().upper(),
            destination=str(args.get("destination") or "").strip().upper(),
            booking_reference=str(args.get("booking_reference") or "").strip(),
            extra_expenses=str(args.get("extra_expenses") or "").strip(),
            their_address=str(args.get("their_address") or "").strip(),
        )
        out_dir = plugin_dir / "letters"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = out_dir / f"delayclaim-{stamp}.md"
        path.write_text(text, encoding="utf-8")
        return _json({
            "ok": True,
            "eligible": True,
            "letter": text,
            "path": str(path),
            "amount": a.amount,
            "currency": a.currency,
            "sent": False,
            "note": "Drafted only. Nothing was sent: review it, then send it yourself and record "
                    "that with delayclaim_case_update status=sent.",
            "assessment": a.as_dict(),
        })

    def case_open_handler(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            a = _build_assessment(args)
        except Exception as exc:  # noqa: BLE001
            return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        now = _now()
        ref = f"DC-{datetime.now(timezone.utc).strftime('%y%m%d')}-{abs(hash((now, a.distance_km, args.get('origin')))) % 10000:04d}"
        conn = _connect(home())
        try:
            with conn:
                conn.execute(
                    """INSERT INTO cases (reference, passenger, carrier, flight_number, origin,
                       destination, via, flight_date, kind, regime, distance_km, band,
                       per_passenger, currency, amount, eligible, reason, status, notes,
                       created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        ref,
                        str(args.get("passenger") or "").strip(),
                        str(args.get("carrier") or "").strip(),
                        str(args.get("flight_number") or "").strip(),
                        a.distance_km and str(args.get("origin") or "").upper(),
                        str(args.get("destination") or "").upper(),
                        str(args.get("via") or "").strip(),
                        str(args.get("flight_date") or "").strip(),
                        a.kind,
                        a.regime,
                        a.distance_km,
                        a.band,
                        a.per_passenger,
                        a.currency,
                        a.amount,
                        int(a.eligible),
                        a.reason,
                        "draft",
                        str(args.get("notes") or "").strip(),
                        now,
                        now,
                    ),
                )
                row = conn.execute("SELECT * FROM cases WHERE reference = ?", (ref,)).fetchone()
        finally:
            conn.close()
        return _json({"ok": True, "case": _row_summary(row), "assessment": a.as_dict(),
                      "sent": False, "disclaimer": _DISCLAIMER})

    def case_update_handler(args: dict[str, Any], **kwargs: Any) -> str:
        ref = str(args.get("reference") or "").strip()
        if not ref:
            return _json({"ok": False, "error": "reference is required"})
        fields: dict[str, Any] = {}
        for key in ("status", "sent_on", "their_reply_on", "outcome", "notes"):
            value = args.get(key)
            if value is not None and str(value).strip():
                fields[key] = str(value).strip()
        if not fields:
            return _json({"ok": False, "error": "nothing to update: pass status, sent_on, their_reply_on, outcome or notes"})
        fields["updated_at"] = _now()
        conn = _connect(home())
        try:
            with conn:
                existing = conn.execute("SELECT * FROM cases WHERE reference = ?", (ref,)).fetchone()
                if existing is None:
                    return _json({"ok": False, "error": f"no case with reference {ref!r}"})
                assignments = ", ".join(f"{k} = ?" for k in fields)
                conn.execute(f"UPDATE cases SET {assignments} WHERE reference = ?", (*fields.values(), ref))
                row = conn.execute("SELECT * FROM cases WHERE reference = ?", (ref,)).fetchone()
        finally:
            conn.close()
        return _json({"ok": True, "case": _row_summary(row)})

    def case_list_handler(args: dict[str, Any], **kwargs: Any) -> str:
        limit = max(1, min(int(args.get("limit") or 20), 200))
        status = str(args.get("status") or "").strip()
        conn = _connect(home())
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM cases WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM cases ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
            totals = conn.execute(
                """SELECT currency, SUM(amount) AS claimed,
                          SUM(CASE WHEN status IN ('paid') THEN amount ELSE 0 END) AS recovered,
                          COUNT(*) AS n
                   FROM cases GROUP BY currency"""
            ).fetchall()
        finally:
            conn.close()
        return _json({
            "ok": True,
            "cases": [_row_summary(r) for r in rows],
            "totals": [
                {"currency": t["currency"], "claimed": t["claimed"] or 0,
                 "recovered": t["recovered"] or 0, "cases": t["n"]}
                for t in totals
            ],
        })

    def airport_handler(args: dict[str, Any], **kwargs: Any) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            return _json({"ok": False, "error": "query is required"})
        hits = lookup_airport(query, limit=int(args.get("limit") or 8))
        return _json({"ok": True, "query": query, "airports": hits, "count": len(hits)})

    return {
        "delayclaim_assess": assess_handler,
        "delayclaim_letter": letter_handler,
        "delayclaim_case_open": case_open_handler,
        "delayclaim_case_update": case_update_handler,
        "delayclaim_case_list": case_list_handler,
        "delayclaim_airport": airport_handler,
    }
