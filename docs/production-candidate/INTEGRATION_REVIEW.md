# TREX integrated review and next-version handoff

Date: 2026-09-25
Candidate: **desk-wave3-safety-rc1**
Verified upstream base: **ab493d7b0758ebb04611d7b5f842a47049b2e77f**
Decision: **reviewable shadow/evidence candidate; no new autonomous execution or deployment authorization**

## 1. Reconciliation of all three sessions

This review combines the original Wave 3 transcript, the two additional Claude Code sessions, the uploaded main archive, and direct GitHub reads. Transcript statements are evidence of what an agent reported, not independent proof of current deployment or completed tests.

| Input | Useful established context | Integration treatment |
|---|---|---|
| `Pasted text(20260925-183104).txt` | Wave 3 plan, four reported worktrees, legacy price-revision defect, optional assignment rule, preview proposal, capture/what-if observations | Review the proposed behavior; independently reproduce source defects; do not infer that unavailable commits were merged or deployed |
| `Pasted text(20260925-183546).txt` | Wave 2 landing, missing-signal retry fix, 37 capture names versus sealed 36-name rank universe, shared editable-environment incident, viewer work | Preserve the existing implementation and sealed roster; isolated environments/local gates, no duplicate cockpit rewrite |
| `Pasted text (2)(20260925-183557).txt` | Adaptive T-NULL amendments, small-cell NOT_EVALUABLE policy, research registry/holdout custody, quota-aware campaign execution, no hosted Actions | Preserve historical records; no new research executions or automatic promotions; later committed report governs final campaign disposition |
| Uploaded main and connected GitHub | Main at `ab493d7`; merged campaign report includes later sealed outcomes | Build on this source, not an earlier transcript's still-open inner-fold decision queue |

### Remote custody discrepancy

The first transcript concludes that `feat/desk-w3-shadows` (`b6c5b26`), `fix/legacy-late-fill-price` (`7e7d82c`), `feat/desk-assignment-exit` (`a5ca17d`), and `feat/desk-w3-enter` (`f96275d`) were pushed and clean, but not landed on main. The connected branch listing returned 17 branches, including main at the expected hash but none of those four. Direct commit/file reads for all four reported abbreviated hashes returned no matching commit (404 or 422). A final branch-list recheck returned the same main and omissions.

This is an unresolved custody problem, not proof of deletion or dishonesty. Preserve the local worktrees on the real host and verify the push remote before any cleanup. Recovery should use full commit IDs, `git remote -v`, `git branch -vv`, `git ls-remote`, clean-tree checks and an explicit bundle of each branch from that host. Do not delete their only local copies based on the transcript's “everything pushed” summary.

**The supplied patch is an independent candidate against main, not a merge of the exact missing branch bytes.** If those branches are recovered, compare each lane against this candidate and take one coherent implementation of shared files. The main archive has no Git history; the local synthetic baseline used to generate the patch is not an upstream commit.

## 2. What changes the production assessment

### A. Queue validation must follow the actual producer

The transcript changed an identity check after concluding that the emitted structure did not contain `deal_id`. The actual `desk/miner.py` producer supplies it. The new consumer checks both `structure.id` and `structure.deal_id` against the outer ID. A test runs the actual miner and parses its output, rather than relying only on an invented queue fixture.

The admission boundary also verifies the envelope session, next-session entry date, exact entry/validity times, active policy hashes, rank sequence, duplicate IDs, monetary string types, identical legs, quantity, protective structure model, deadline equality and expiry buffer. Maximum loss is recomputed from the actual capped structure, not trusted from the outer JSON and not substituted with a fill-based estimate. Invalid nested objects fail with stable error codes instead of being coerced into a plausible structure.

This establishes internal consistency; it does not establish the economic correctness of a signal or authorize a trade.

### B. A “shadow spec” must not enter an executable spool

The E6 draft mixed shadow-mode spec publication with a future runtime that would consume specs. That interface makes a later deployment capable of executing artifacts that were never admitted under fresh broker rails. Checking that `book.json` exists would not repair that boundary.

The candidate records blocked previews in a separate SQLite object kind. It never emits executable specs or mutates the broker book. `--armed` is refused before persistent writes. A missing risk snapshot, missing reconciliation, unresolved margin, missing live exit inputs and the PROPOSED policy are explicit blockers. HALT/AUTO_OFF are additional blockers, not activation tokens. A future E5 must build fresh executable intents from an authorized source; it must not reinterpret `admission_preview` records as orders.

`DESK_PAPER_DIR` already selects the research paper-trades panel. The new preview uses `TREX_DESK_RUN_DIR` for stop-file lookup, avoiding a namespace collision between research and execution state.

### C. Forward evidence cannot borrow from the future or invent an exit

The early D7 draft treated an on-disk future chain as proof that the deadline had passed, and proposed resolving a missing deadline with an older quote. Neither is acceptable for a forward evidence gate: an evaluation clock must bound visible data, and a stale valuation is not an observed exit-day valuation.

The candidate bounds evaluation by the supplied aware clock/session, rejects future-received/source observations, matches the underlying's last-trade session, checks leg identity including expiry, and uses only valid exact-date deadline prices. Invalid data is recorded as a quality issue. Missing/conflicted outcomes are censored, not counted as zero, a win, or a resolved trade.

It retains every candidate, including rejected candidates with validation diagnostics. Cohorts select one usable representative per underlying, row, tier, policy lineage and week. A late-discovered older queue cannot silently change an already-selected historical cohort. Retrospective reconstruction is explicitly labeled and not misrepresented as a timely forward registration.

### D. Twenty resolved examples do not establish an investable strategy

The research session already recognized that sparse cells need a NOT_EVALUABLE classification. Applying the same discipline to D7 means separating sample count from a statistical or investment decision. This candidate displays a 20-resolved sample floor but keeps `promotion_ready` false; it does not implement autonomous pause/retire policy.

The cards are explicitly deadline-EOD proxies. They do not reproduce the full intraday exit path, market depth, fills or assignments. They show gross modeled P&L, assumed commissions and modeled net separately, and retain missing-outcome counts and backfill counts. The first subsequent mark is not called “execution slippage,” because it also contains market movement. No adaptive threshold has been tuned from these fixture outcomes.

A future promotion study needs a registered estimand, costs, dependence-aware uncertainty, missing-data sensitivity, a fixed forward window and a separate operator ruling. Existing amended research must remain labeled as amended; post-observation amendments do not become untouched preregistration merely because they were authorized.

### E. Zero paper what-if fields are an unresolved observation

The transcript's probe reports zeros in several margin fields and concludes that paper what-if is vacuous. Its raw probe/receipt was stored outside the tracked repository and is not available in the archive. This review cannot verify the account type, raw response, order/account configuration, warning fields or before/after account values.

IBKR documents what-if as an estimate of the account's post-trade margin impact, with multiple fields, not a universal position-risk certificate [W1]. The safe conclusion is **margin verification is incomplete**. Do not change a broker rail to accept arbitrary zero responses or silently substitute width-based modeled risk. Modeled maximum loss and broker-reported incremental margin answer different questions. The candidate changes neither the sealed risk limits nor the broker-margin adapter.

Obtain a narrowly scoped paper-account diagnostic with explicit account binding, positive controls, raw warning/unset/error handling, known contracts and tick-valid prices. Retain a private, redacted receipt outside public logs. This candidate sends no what-if requests.

### F. Runtime/accounting boundaries remain important

The existing legacy entry/exit merge logic ignored a revised average when cumulative filled quantity stayed unchanged. Four behavioral tests first reproduced the stale average against the baseline. The fix adjusts only the current tracked order's notional contribution, leaves quantities unchanged, persists its checkpoint and records an event. Replacement-order blending and replay are tested.

This does **not** solve corrections arriving after an order has been removed from the tracked set. Durable execution IDs, completed-order reconciliation, commission corrections, account identity and restart recovery still belong in E5. The new pure assignment heuristic similarly is not a live risk service: dividend/short-call observations must be provided by a validated runtime. Missing dividend or spot data is not evidence of safety. Options assignment can occur outside this one heuristic's trigger; the OIC explicitly warns that assignment is not reliably predictable [W2].

Existing HALT behavior was not redesigned. In the legacy monitor it can suppress new exit placement; do not rename that behavior mentally to “entries off, exits always continue.” A future runtime needs distinct, tested operational states and a supervised emergency procedure.

## 3. Preserved work and research decisions

The additional Wave 2 session located the real shared-environment writer: a test invoked `uv run --frozen`, which can still synchronize the project environment. Its eventual correction used the current interpreter and an explicit test import root; the service units pin their source imports. Official uv documentation distinguishes freezing lockfile updates from opting out of environment synchronization [W3]. Those existing fixes are preserved. New gate/unit templates avoid runtime package synchronization and use an isolated release interpreter.

The existing miner's missing/stale-signal behavior remains retryable; it must not seal an empty queue when the signal producer has not finished. The 37-name capture universe and sealed 36-name XSMOM ranking roster are left unchanged. Existing viewer/cockpit work is retained; the new page attaches to the same FastAPI app without a parallel server or React rewrite.

The committed `docs/campaign-2026-09/REPORT.md` includes a later round-2 sealed evaluation than the second extra transcript's pending approval screen. Its final disposition is no adoption. This build does not reopen any consumed window, alter the selection file's PROPOSED state, regenerate stamps, re-run campaigns, activate `agent-exec`, or rescue data-gated options arms using an improvised proxy. Original files under `data/`, `docs/theory/`, `docs/desk/`, and the dependency/protocol inputs are checked against the input baseline in the verification report.

The long-dated capture failure should be recorded as a terminated/resumable job with preserved cache, not as a permanently impossible capture. The transcript ultimately attributes repeated termination to host memory-pressure preemption; early timeout explanations were revised. Host guard policy and accumulated request accounting need inspection on the actual host. Do not bypass the guard or add repeated blind relaunches. First inventory cache hashes, complete manifests, per-run/cumulative budgets and the actual termination receipt; then size/chunk the work under the existing host-work admission system.

DESK-BT-001 readiness must come from usable recorded-session coverage and implemented study machinery, not a calendar guess such as “October 20” or “October 21.” The missing 2026-09-23 chain and future vendor gaps remain explicit coverage facts; this release does not fabricate or fetch their replacements. SEC contact configuration and any credential rotation remain operator-owned actions, not completed work in this review.

## 4. Actual acceptance evidence

See `verification/VERIFICATION.json` and accompanying logs. The final focused command runs the new safety suite together with existing miner, enforcement, playbook, selection, engine, legacy execution and web tests. It is not the complete repository gate. The available environment lacks the locked interpreter/dependency set and Hypothesis/ruff/mypy, and a broad-suite attempt could not complete under those limitations. Those failed attempts are not reclassified as passing.

The new tests cover real-miner interoperability, malformed shapes/money/identity/date disagreement, rollback and idempotence, audit tampering, backup publication, cohort ordering, future observation rejection, source-session matching, censoring, preserved candidate denominators, explicit arming refusal, kill-file handling, legacy fill revisions, assignment decisions and read-only API behavior. Fixtures derive session relationships and leg keys from the fixture calendar/specifications, while price/accounting expectations remain independently computed numerical oracles.

Desktop and mobile visual fixture renders execute the actual page script against synthetic responses. They found and fixed a mobile grid overflow. HTTP browser navigation was blocked by the environment, so these renders are not a browser/network/CSP end-to-end certificate. TestClient verifies the API routes independently. Systemd calendar expressions parse; actual user-service sandbox support, resource sizing, installation and soak are not verified.

No account login, order, cancellation, exercise, service restart, remote push, branch merge or sealed research run was performed.

## 5. Next coding-agent work, in dependency order

### Gate 0 — recover source and establish one integration owner

Recover the full local hashes/bundles for the four reported lanes. Compare with this patch in a separate worktree; settle shared `desk/__main__.py`, engine and legacy accounting changes deliberately. Run the canonical isolated environment gate with the actual import root recorded. Fix lint/type/full-suite issues rather than waiving them. Obtain independent adversarial review before any landing. Preserve the live checkout and its monitor's import environment throughout.

### Gate 1 — evidence deployment and recovery rehearsal

Use copied real inputs first, check the exact-session/censoring behavior and the all-candidate counts, and verify empty-valid versus missing/invalid queues. Exercise a process interruption, replay and backup restoration. Measure database growth, historical verification latency and memory on the real store before adopting the proposed unit limits. Establish off-host backups/audit anchors. Install only the new evidence/preview units after operator review; verify JSON freshness/readiness as well as systemd exit codes.

### Gate 2 — E5 paper broker runtime, not an extension of the preview

Implement a single broker owner with explicit paper account/client identity and namespaced order references, a durable intent/outbox and execution-correction ledger, and restart reconciliation over positions, open orders and executions. Exactly-once broker submission cannot be assumed from a local transaction: ambiguous acknowledgments must reconcile before retrying.

Unknown exposure blocks entries. Missing protective legs require a visible halt and a defined supervised response. Entries require fresh account/market/risk snapshots and authoritative shared rails; a stale or missing input cannot be transformed into an empty/zero exposure. Separate “entries disabled” from “all automation halted,” keeping intended exit behavior explicit. Feed the pure exit engine fresh dividends, spot and per-leg quotes and verify real broker margin without weakening modeled caps. Test reconnects, partial fills, late fills during cancellation, corrections after terminal status, duplicate callbacks and recovery with conflicting broker/local state.

Activation must be a separate, expiring operator decision bound to release, account, policy hashes and permitted quantity/risk. The PROPOSED hash-pinned selection file is not itself that decision. No existing shadow artifact may bypass it.

### Gate 3 — D8 and study validation

Keep any news/LLM layer veto-only and initially observational. Retain source publication/retrieval times, model/version/prompt hashes and a reproducible decision receipt; fail closed when required inputs are unavailable. Do not let a language model authorize risk, classify its own forecasts as successful, or change a frozen research protocol. Implement the registered BT001/fidelity and coverage checks separately; no new scoring of already-consumed windows.

### Gate 4 — supervised paper rollout decision

Only after the preceding gates, actual broker reconciliation, verified margin inputs, exit-input wiring and a documented shadow soak should the operator consider the planned quantity-one/one-deal-per-day paper pilot. That limit is a proposed rollout bound, not an instruction executed here. An operational paper pilot does not establish strategy profitability or live-capital readiness. Real-capital use requires a separate risk and suitability decision.

## Sources and authority

[S1] Supplied original Wave 3 transcript, especially the final branch/status section and explicit maintainability feedback.
[S2] Additional `Pasted text(20260925-183546).txt`, the final diagnosis of the shared editable install, Wave 2 landing and missing-signal retry.
[S3] Additional `Pasted text (2)(20260925-183557).txt`, calibration amendments, NOT_EVALUABLE policy, research authorization and local-gate rules.
[S4] Actual source at the stated upstream base: `desk/miner.py`, `desk/paths.py`, `desk/selection.py`, `trex/plan.py`, legacy entry/monitor, campaign `REPORT.md` including sections 7–8.
[S5] Connected GitHub branch listing and attempted reads of all four claimed commits, checked 2026-09-25. This establishes retrievability through this connection, not the absence of private local worktrees.

[W1] Interactive Brokers, “Test Order Impact (WhatIf),” accessed 2026-09-25: `https://www.interactivebrokers.com/docs/tws-api/doc/orders/test-order-impact-what-if`.
[W2] Options Industry Council, “Options Assignment,” accessed 2026-09-25: `https://www.optionseducation.org/referencelibrary/faq/options-assignment`.
[W3] Astral uv, “Locking and syncing,” accessed 2026-09-25: `https://docs.astral.sh/uv/concepts/projects/sync/`.
