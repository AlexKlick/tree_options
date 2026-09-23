# Cockpit flash review — 2026-09-23 (M8)

The market-desk campaign's last slice: a small model drives a real browser
through every cockpit page at phone, laptop and widescreen sizes; its
output is treated as **leads**, a second model verifies them against the
code, and only verified findings are fixed (test first).

## Method

- **Reviewer:** `glm-5.3-flash` through `claude-zai -p --model glm-5.3-flash`,
  Playwright MCP only (`--allowedTools mcp__plugin_playwright_playwright`),
  read-only prompt (never click/type/submit). Harness:
  `scripts/cockpit_flash_review.sh` (gated by
  `scripts/cockpit_flash_review_when_free.sh`).
- **Coverage:** 6 routes (`#/`, `#/discover`, `#/stats`, `#/market`,
  `#/market/SPY`, `#/plan/putspread-20260922`) × 3 viewports (390×844,
  1600×900, 2360×984) × **2 independent runs**. Each viewport: resize,
  navigate twice (hash-only changes do not reload), screenshot, a11y
  snapshot, an overflow / sub-40px-target evaluation, console errors,
  network requests.
- **Model proof:** every run wrote `<lane>-rN.models` from the CLI's own
  `modelUsage`; all 12 read exactly `["glm-5.3-flash"]`. The first relaunch
  did NOT: `ZAI_MODEL=glm-5.3-flash claude-zai` loses to the launcher's
  `~/.claude/.env` (`ZAI_MODEL=glm-5.3[1m]`), so plans/discover ran on full
  glm-5.3. Those runs were stopped and archived as extra leads; the
  harness now passes `--model` and refuses any run not billed to flash.
- **Promotion rule:** a finding is promoted only if both runs of a lane
  reported it. Single-run leads that are objectively checkable were sent
  to verification too, labelled as single-run.
- **Verification:** Codex `gpt-6-astra`, read-only, against the code at
  `66ac7d5` — verdict, root cause (file:line), minimal fix for each lead.
- Clean at every viewport of every route: document-level horizontal
  overflow 0, console errors 0, all requests same-origin 2xx.

## Findings

| id | lane(s) | finding | runs | Codex verdict | root cause |
|---|---|---|---|---|---|
| F1 | performance | equity chart labels clipped ("$1,000,27", lost "$") at all viewports | 2/2 | REAL | fixed 64-unit gutters in `TimeSeriesChart` |
| F2 | performance | equity y-axis from $0: a ~$12 move draws flat against the top | 2/2 | REAL | `y_extent()` always includes zero (P&L rule applied to a level series); symbol price chart too |
| F3 | plans, discover, performance, plan | phone tables: 600–707px tables in 336px scrollers, no cue, "Max gain" reads "Ma" | 2/2 | PARTIAL (scrollbar visibility is the browser's; the missing cue is real) | `.table-scroll` min-width 600px, no affordance |
| F4 | plans, market, symbol | "● live" beside hours-stale / "delayed" data | 2/2 | PARTIAL (it meant "API answered within 45s"; wording misleads, and it showed before the first response) | `AppShell` PollBadges |
| F5 | plans, plan | ages in raw seconds ("account 16608s ago", "33243s ago — stale") | 2/2 | REAL | five ad-hoc age formatters, two seconds-only |
| F6 | market, symbol, plan | phone tap targets 19–25px: proposal tickers, "← Market", "← All plans", book.json / events.jsonl | 2/2 | REAL | the mobile ≥40px rule covered chips/nav/toggle only |
| F7 | symbol | IV30 "11.4" with no unit | 2/2 | REAL | `SymbolPage` (also the market cards) |
| F8 | plan | "Live marks … refresh ~20s" above "33473s ago — stale, monitor not refreshing" | 2/2 | REAL | unconditional heading copy |
| S1 | performance | "net liquidization" typo | 1/2 | REAL | copy |
| S2 | plan | oct and nov 185/150 payoff sliders + headings share one accessible name | 1/2 | REAL | name omits the structure id |
| S3 | plans (glm-5.3 run) | tile "Unrealized (open) +$1" vs table "-$3" | 1 (full-model run) | PARTIAL → **real P&L error** | tile valued the monitor's CENT-ROUNDED mark: 0.18/0.21 → mid 0.195 stored "0.20"; 1.22/1.29 → 1.255 stored "1.26". True −$3, tile +$1. The table also mixed filled vs open quantity after partial exits |
| S4 | performance | chart labels ~6px on a 336px phone chart | 1/2 | REAL | fixed 640-unit viewBox scaled text |

**Correction:** the M7 responsive sweep (2026-09-22) reported "0 sub-40px
tap targets". F6 shows that was wrong — three routes had 19–25px links
that no CSS rule covered.

## Fixes

`1ee1d30` (S3) · `8834b19` (F1/F2/S4) · `8a0fa9f` (F3–F8, S1, S2). Every
fix landed test-first (the new tests failed on the old code). One
existing test pinned the old $0 floor for equity and was updated to the
new contract. Gate on the final tree, run through the host RAM admission
(`host-work --profile test`): pytest **2758 passed / 0 failed**; ruff +
mypy clean (166 files); web `npm run check` **82/82** vitest, tsc
clean, build + single-chunk check ok.

- **S3** — `positions.open_unrealized()`: one calculation for the tile,
  the plan card and the net-positions table: unrounded bid/ask mid ×
  open quantity; falls back to the stored mark, then the monitor's
  filled-basis figure rescaled to open quantity.
- **F1/F2/S4** — `series.level_extent()` (data ± 8%, never zero-anchored;
  flat band scales with the level) for equity and daily closes;
  `TimeSeriesChart` draws at its measured pixel width (ResizeObserver),
  sizes the left gutter to the widest label, flips the last-point label
  inside, draws the $0 line only when $0 is in range, height 200–320px.
- **F5** — `ago()` in `lib/format.ts`: 42s · 8m · 4h 37m · 2d 2h; used by
  the portfolio chips, marks caption, scan pill, proposals line, market
  and symbol age notes.
- **F4** — "● API connected" / "○ API slow" / "○ loading"; data ages stay
  in their own pills.
- **F7/F8/S1** — IV30 "%" (symbol tile + market cards); "Latest marks
  (… the monitor targets ~20s; age below)"; typo.
- **F3** — `TableScroll`: when a table overflows, the cut edge fades
  (mask) and "scroll for more columns →" shows; inert where it fits.
- **F6** — `.tap-link` (inline-flex, min-height 40px on phones) on the
  proposal tickers, back links and raw-file links.
- **S2** — structure id in the payoff heading and the slider's name.

## Live verification (deployed, 2026-09-23 ~05:27 MDT)

API: portfolio tile, plan card, net-positions table and the monitor's
filled total all read **−$3.00** (tile was +$1). Equity axis
$1,000,269.20–$1,000,283.12 over data $1,000,270.16–$1,000,282.16 (was
$0–$1,000,282). TSM daily-close axis $303.61–$490.46 (was $0-based).

Browser (Playwright, bundle `index-QMa-JzhY.js`, all 6 routes; for the
price chart `#/market/TSM`, because SPY had no bars cached):

| check | 390×844 | 1600×900 | 2360×984 |
|---|---|---|---|
| document overflow | 0 on all routes | 0 | 0 |
| chart labels outside their svg | 0 | 0 | 0 |
| time-series chart height | 200px | 320px (was 672) | 320px (was ~590) |
| time-series label box height | 17px (was ~6px text) | 17px | — |
| links/buttons under 40px | **0** (was 10 market, 1 symbol, 3 plan) | n/a | n/a |
| overflowing tables with cue | 8/8, "scroll for more columns" ×8 | none overflow | none overflow |
| raw-second ages | 0 ("marks 15h 12m ago", "account 11h 42m ago", "scan 12h 34m ago") | 0 | 0 |
| header badge | "● API connected" | same | no "● live" anywhere |

Also: IV30 "33.0%" / market card "iv30 31.2%"; heading "Latest marks
(… the monitor targets ~20s; age below)"; sliders "(nvda-oct)" /
"(nvda-nov)"; no "liquidization"; 0 console errors across the session.

## Not fixed (single-run, unverified leads)

Expiry shown as raw `20261016`; `/api/plans` fetched twice on mount;
proposal confidence "90%" unlabelled; "2 new" with no per-item marker;
market card headline price unlabelled (mid); strike thresholds without
"$"; `PayoffChart` shares S4's fixed-viewBox text scaling.

## Evidence

Outputs: `~/.local/state/trex-flash-review/` (`<lane>-r{1,2}.{md,json,models,err}`,
`glm-5.3-full-partial/`, `codex-verify.md`).
