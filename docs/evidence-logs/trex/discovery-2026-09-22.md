# Discovery + portfolio deployment evidence — 2026-09-22

Commits C0..C15 pushed through `7084924+` (main). Gates at each commit:
ruff + mypy (158 files) + pytest (2554 passed, dot-count verified);
frontend tsc + vitest 42/42 + vite build.

## Live end-to-end (loopback)

- Services: `trex-web` + `trex-monitor` restarted (C1 accounting fix +
  C9 account writes live; monitor re-armed, heartbeat fresh),
  `trex-discovery` started (clientId 74, serve loop, auto 16:11 ET).
- Monitor wrote `account.json` in its run dir on the first cycle.
- `GET /api/plans`: portfolio block (open 8, committed $477,
  unrealized_open +$1 on the open basis, unrealized_filled −$3 on the
  filled basis — both named, both honest), account DUT143714 NLV
  $1,000,285.48 / cash $999,516.91 / BP $3,998,067.63 age 41s,
  accounts_seen [DUT143714].
- Scan-on-demand round trip: POST → 202 `7a4e1dfaa5ce` → runner claimed
  (behind the in-flight auto scan) → manual scan completed →
  `scan.result` status ok → latest.json mode=manual with the matching
  request id. Auto scan had fired first (16:11 due at service start).
- Discovery state: 5/5 underlyings scanned (NVDA/QQQ/SPY/AMD/MU), 91
  rows quoted / 0 unquoted, 11 candidates accepted, 168 rejected with
  per-row tri-state rule audits. Top of book: QQQ 657/642 w15 debit
  $0.20 (75.9:1), QQQ 658/643 w15 (72.2:1), SPY 715/700 w15 (65.7:1).
- Browser (Playwright, 1600px): `#/discover` — freshness pill from the
  payload age, ranked table + Rejected section with collapsed audits,
  no horizontal overflow; `#/` — portfolio tiles + account card.
- Tailnet boundary: anonymous `/trex/` and `/trex/api/discovery` →
  403 sign-in HTML (no JSON leak). Authenticated render is the
  operator's check (Google login not automatable).

## Fixes forced by verification

- engine `max_loss` sign aligned with the book convention (−debit×100)
  after the live table showed "+$19" as a max loss.
- (earlier, C4) probe expiry selection now filters to the DTE window —
  `expirations[0]` was 0DTE and unqualifiable at 15:52 ET.
