# Desk macro calendar: append-only seal log

Each row seals one ``macro-*.json`` (sha256 of the file bytes, also in the
``.sha256`` sidecar). ``tree_options.desk.events.load_macro`` refuses a file
whose bytes do not match. Rows are only ever appended (``python -m
tree_options.desk seal-macro``); never edit or delete a row.

## Provenance per kind (who/what enters each item)

- **fomc**: parsed by ``tree_options.desk.events.parse_fomc`` from
  https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm (one GET
  on 2026-09-23, HTTP 200, 165,460 bytes; the 2026 and 2027 year panels).
  The decision date is the meeting's last day; ``sep`` marks the asterisked
  meetings ("Meeting associated with a Summary of Economic Projections").
  The weekly ``update-events`` job re-reads the page and writes
  ``<state>/events/macro-drift.json`` (exit 1) if it ever differs.
- **cpi**, **nfp**: EMPTY. Operator/agent entry required. bls.gov answered
  the scripted GET of https://www.bls.gov/schedule/news_release/cpi.htm
  with HTTP 403 on 2026-09-23, and no date was entered from memory. To add
  them: put ``{"date", "source", "entered_by"}`` items into the arrays by
  hand (source = the BLS schedule page or release you read), then run
  ``seal-macro``; the items are carried and the new row records the counts.
- **opex**: computed, the third Friday of each month
  (``time.monthlies.is_monthly_expiry``), or the session before it when
  the exchange is closed (2026-06-19, Juneteenth, gives 2026-06-18), on the
  trex NYSE calendar (``data/calendar/trex/``, through 2028-12-29).
- **vix_expiry**: computed, 30 calendar days before the next month's opex
  date (``time.expiries.minus_calendar_days``), or the session before when
  that day is a holiday (Cboe's VIX final-settlement rule).

| sealed (UTC) | file | sha256 | counts | basis |
|---|---|---|---|---|
| 2026-09-23T21:18:12Z | macro-2026-2027.json | 2aef5baf0dbf1abadb8ef13b3c01c9ebc37dc43afe363204e947d785a2c6f13b | fomc=16 cpi=0 nfp=0 opex=24 vix_expiry=24 | FOMC parsed by events.parse_fomc from https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm (saved page fomc.html, fetched 2026-09-23); cpi 0 / nfp 0 items carried as hand-entered (entered_by per item); opex + vix_expiry computed on the NYSE session calendar |
| 2026-09-23T23:43:03Z | macro-2026-2027.json | 1698b53c01cc696d92f2356dbdb3078ff4ef206955a5b1e21d0a9c3f905e3209 | fomc=16 cpi=12 nfp=12 opex=24 vix_expiry=24 | FOMC parsed by events.parse_fomc from https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm (saved page fomc.html, fetched 2026-09-23); cpi 12 / nfp 12 items carried as hand-entered (entered_by per item); opex + vix_expiry computed on the NYSE session calendar; CPI/NFP 2026 hand-entered (24 items: claude-main read bls.gov cpi.htm + empsit.htm + schedule/2026/home.htm, 24/24 agree; lane re-read cpi.htm + empsit.htm, 24/24 match); 2027 not yet published by BLS |
