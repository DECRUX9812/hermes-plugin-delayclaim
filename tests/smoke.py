import sys
sys.path.insert(0, '.')
from delayclaim.engine import resolve_regime, leg_distance_km, Leg, assess
from datetime import datetime

print('regimes:', resolve_regime('DE', 'US', 'union'), resolve_regime('GB', 'US', 'uk'),
      resolve_regime('US', 'DE', 'union'), resolve_regime('Turkey', 'DE', 'nonunion'))

for o, d, via in [('BER', 'JFK', ()), ('BER', 'MUC', ()), ('LHR', 'JFK', ()),
                  ('BER', 'JFK', ('FRA',)), ('CDG', 'IST', ())]:
    dist = leg_distance_km(Leg(o, d, via))
    print(f'{o}->{d} via {via or "-"}: {dist:.0f} km')

s = datetime(2026, 10, 3, 14, 0)
a = datetime(2026, 10, 3, 19, 20)
r = assess(origin='BER', destination='JFK', regime='eu261', kind='delay',
           scheduled_arrival=s, actual_arrival=a, passengers=2, carrier_kind='union')
print('verdict:', r.eligible, r.amount, r.currency, r.band, r.delay_hours)
print('reason:', r.reason)
