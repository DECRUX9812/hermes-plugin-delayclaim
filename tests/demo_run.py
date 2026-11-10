"""The 90-second demo path, runnable from the repo root.

Assesses a real itinerary, drafts the letter, opens the case, marks it sent,
and prints the case list. Exactly what the video shows.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delayclaim.tools import make_handlers

PLUGIN = Path(__file__).resolve().parents[1]
HOME = Path(__file__).resolve().parent / "tmp-home"
h = make_handlers(PLUGIN, lambda: HOME)


def show(title, payload):
    print(f"\n## {title}")
    print(json.dumps(payload, indent=2, ensure_ascii=False)[:1200])


FLIGHT = dict(
    origin="BER",
    destination="JFK",
    kind="delay",
    scheduled_arrival="2026-10-03T14:00",
    actual_arrival="2026-10-03T19:20",
    regime="auto",
    carrier_kind="union",
)

show("1. WHAT IS OWED", json.loads(h["delayclaim_assess"](dict(FLIGHT, passengers=2))))

show(
    "2. THE LETTER",
    json.loads(
        h["delayclaim_letter"](
            dict(
                FLIGHT,
                carrier="Lufthansa",
                flight_number="LH400",
                passenger="Ritesh Patel",
                booking_reference="ABC123",
                passengers=2,
                extra_expenses="Taxi to hotel: EUR 48\nMeals for two: EUR 31",
            )
        )
    ),
)

opened = json.loads(
    h["delayclaim_case_open"](
        dict(
            FLIGHT,
            carrier="Lufthansa",
            flight_number="LH400",
            passenger="Ritesh Patel",
            flight_date="2026-10-03",
            passengers=2,
        )
    )
)
show("3. THE CASE", opened)
ref = opened["case"]["reference"]

show(
    "4. SENT",
    json.loads(h["delayclaim_case_update"](dict(reference=ref, status="sent", sent_on="2026-11-10"))),
)
show("5. WHERE IT STANDS", json.loads(h["delayclaim_case_list"](dict(limit=5))))
