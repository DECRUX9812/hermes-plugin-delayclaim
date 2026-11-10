"""Tool-layer tests: the handlers Hermes actually calls."""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delayclaim.tools import make_handlers  # noqa: E402

PLUGIN_DIR = Path(__file__).resolve().parents[1]
TMP_HOME = Path(__file__).resolve().parent / "tmp-home"


def handlers():
    TMP_HOME.mkdir(exist_ok=True)
    return make_handlers(PLUGIN_DIR, lambda: TMP_HOME)


def call(name, **args):
    return json.loads(handlers()[name](args))


def test_assess_tool():
    r = call("delayclaim_assess", origin="BER", destination="JFK", kind="delay",
             scheduled_arrival="2026-10-03T14:00", actual_arrival="2026-10-03T19:20",
             passengers=2, regime="eu261", carrier_kind="union")
    assert r["ok"] and r["eligible"]
    assert r["amount"] == 1200 and r["currency"] == "EUR"
    assert "Sturgeon" in " ".join(r["citations"])


def test_assess_auto_regime():
    r = call("delayclaim_assess", origin="LHR", destination="JFK", kind="delay",
             scheduled_arrival="2026-10-03T09:00", actual_arrival="2026-10-03T13:30", regime="auto")
    assert r["ok"] and r["regime"] == "uk261" and r["currency"] == "GBP" and r["amount"] == 520


def test_assess_bad_airport_is_a_clean_error():
    r = call("delayclaim_assess", origin="ZZZ", destination="JFK",
             scheduled_arrival="2026-10-03T14:00", actual_arrival="2026-10-03T19:00")
    assert r["ok"] is False and "unknown airport" in r["error"]


def test_assess_ineligible_says_why():
    r = call("delayclaim_assess", origin="BER", destination="MUC", kind="delay",
             scheduled_arrival="2026-10-03T09:00", actual_arrival="2026-10-03T10:30")
    assert r["ok"] and r["eligible"] is False and r["amount"] == 0
    assert "Sturgeon" in r["reason"]


def test_letter_is_written_and_not_sent():
    r = call("delayclaim_letter", origin="BER", destination="JFK", kind="delay",
             scheduled_arrival="2026-10-03T14:00", actual_arrival="2026-10-03T19:20",
             carrier="Lufthansa", flight_number="LH400", passenger="Ritesh Patel",
             passengers=1)
    assert r["ok"] and r["sent"] is False and r["path"].endswith(".md")
    body = Path(r["path"]).read_text(encoding="utf-8")
    assert "Sturgeon" in body and "Article 7" in body and "600" in body


def test_letter_refuses_when_ineligible():
    r = call("delayclaim_letter", origin="BER", destination="MUC", kind="delay",
             scheduled_arrival="2026-10-03T09:00", actual_arrival="2026-10-03T10:00",
             carrier="Lufthansa")
    assert r["ok"] and r["eligible"] is False and r["letter"] is None


def test_case_lifecycle():
    opened = call("delayclaim_case_open", origin="BER", destination="JFK", kind="delay",
                  scheduled_arrival="2026-10-03T14:00", actual_arrival="2026-10-03T19:20",
                  carrier="Lufthansa", passenger="Test Passenger", passengers=1,
                  flight_date="2026-10-03")
    ref = opened["case"]["reference"]
    assert opened["case"]["status"] == "draft"

    upd = call("delayclaim_case_update", reference=ref, status="sent", sent_on="2026-11-10")
    assert upd["ok"] and upd["case"]["status"] == "sent"
    assert "2027-01-05" in upd["case"]["next_action"]

    listed = call("delayclaim_case_list", limit=5)
    assert listed["ok"] and any(c["reference"] == ref for c in listed["cases"])
    assert listed["totals"]


def test_case_update_unknown_reference():
    r = call("delayclaim_case_update", reference="DC-000000-0000", status="sent")
    assert r["ok"] is False and "no case" in r["error"]


def test_airport_lookup_tool():
    r = call("delayclaim_airport", query="Berlin", limit=3)
    assert r["ok"] and r["count"] >= 1
    assert any(a["iata"] == "BER" for a in r["airports"])
