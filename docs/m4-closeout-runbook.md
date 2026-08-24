# M4 — coverage-era closeout runbook (durable preflight chain)

Operator procedures for closing out the M4-B structural-coverage era and
walking the chain that follows it: census → protocol 0.2.1 amendment →
bars era → G4 sealed-event preflight. The machinery is the
`m4/durable-closeout-preflight-20260823` chain (A1 runstate, A2 census,
A3 amendment, A4 bars launcher, A5 seal guard).

The vendor lane itself (key custody, cache, budget math, resume recipe,
the entitlement-window warning) is documented in
`docs/m4-massive-runbook.md` — this file does not repeat it. Era design
and launch record: `docs/evidence-logs/m4/m4-b-coverage-era.md`. The
sealed-gate plan: `docs/m4-g4-sealed-gate-plan.md`.

All commands below run from the repo root of the checkout that owns the
era artifacts. Every command is marked **read-only** (reports only) or
**mutating** (writes state, artifacts, or both).

## 0. Standing hard rules (memorize before touching anything)

1. **A timed-out or disconnected process is UNKNOWN — never success,
   never FAILED, never blind-retried.** Silence is liveness evidence,
   not outcome evidence. UNKNOWN is a classification an observer makes;
   it is never written into the journal.
2. **Long-running authority NEVER lives in `/tmp`.** `/tmp` is wiped on
   reboot and it already orphaned this era once (the pass-1 log and
   marker at `/tmp/m4h-era-slice1.*` died with a reboot). The G4
   authority ledger mechanically refuses any root that resolves under
   `/tmp`. Durable state lives under the repo's `artifacts/`.
3. **The capture manifest is NEVER repaired.** A pinned-vs-observed
   mismatch is a `MANIFEST_MISMATCH` refusal (§3). Reconciliation is an
   owner decision; hand-editing derived evidence is prohibited.
4. **`scripts/capture_massive_structural.py` is NEVER edited mid-era.**
5. **No GitHub Actions.** The local gate is the authority.
6. **Normal merge only after owner approval.** Every closeout packet is
   owner-gated before it lands.

## 1. Status at a glance

### 1.1 Journaled runs

```
uv run --frozen python scripts/era_status.py --json          # read-only
uv run --frozen python scripts/era_status.py --run-id <run-id> --json   # read-only
```

Reads the most recently written store under `artifacts/runstate/`
(default `--store-root`). The payload names: `state` (journal
projection), `classification` (heartbeat class), `lease` (lease class),
`journal_tail` + `seq` + `tail_damaged` (journal integrity),
`pinned_manifest` (the pinned capture-manifest hash), and
`failure_reason` (only when a FAILED record exists). This command NEVER
mutates and NEVER repairs: a torn projection is reported (exit 2), not
rebuilt — rebuilding is a write and belongs to a lease holder.

### 1.2 The pre-journal legacy coverage era (live now)

The current era predates the journal. Until its store is adopted at
closeout, check it with the legacy discovery form — this scans `/proc`
for the capture process and reports what is actually live:

```
uv run --frozen python scripts/era_status.py \
    --capture-dir artifacts/m4b-coverage-era --json        # read-only
```

While the wrapper is live this prints `"state": "UNKNOWN"` with
classification `UNKNOWN_RESUMABLE` and exits 3 — a run with no journal
is UNKNOWN, never healthy, never FAILED. After the wrapper exits (and
before the store is adopted) it reports NO RUN FOUND (exit 4). When the
journaled store takes over (§4.2), use §1.1 instead.

### 1.3 File-count health signal (pre-journal era only)

The journaled facts are the truth; while they do not exist yet, the
master-file count is the honest progress signal:

```
ls artifacts/m4b-coverage-era/masters | wc -l               # read-only
```

It climbs toward **3,045** masters = 29 underlyings × 105 Fridays, the
count the committed universe manifest declares
(`data/coverage/coverage_universe.json`). The era evidence doc and the
G4 plan say "30 × 105 = 3,150"; that 29-vs-30 discrepancy is RECORDED,
not papered over — owner decision 2026-08-23: the census derives
expected counts from the committed manifest, and the docs are
reconciled by the owner at era-results. Do not "fix" either number by
hand.

### 1.4 Heartbeat classifications and what you may do

Heartbeats are written by a lane's library code via
`tree_options.runstate.store.RunStore.write_heartbeat`. The constants
`HEARTBEAT_INTERVAL_S` (60 s) and `STALE_AFTER_S` (900 s) are the
contract for FUTURE lane wiring — no shipped lane in this PR A writes
heartbeats yet. The pre-journal legacy coverage era (live at the time
of writing) wrote none; era_status classifies the ABSENCE of a beat
in a process state as `UNKNOWN_RESUMABLE` (the safe default — silence
is never `ALIVE`). The heartbeat's recorded state must also match the
journal projection state; a mismatch is `UNKNOWN_RECONCILIATION_REQUIRED`
(round-1 review fix). `era_status` classifies:

| classification | meaning | operator action |
|---|---|---|
| `ALIVE` | fresh beat AND its state matches the journal | none |
| `ALIVE_SILENT` | pid alive, beat stale (60 s/900 s thresholds) | **watch, do not act.** Do NOT kill on silence alone — a lane may write nothing for hours by design |
| `DEAD_TERMINAL` | process gone and the journal already says how it ended | none (read the journal) |
| `UNKNOWN_RESUMABLE` | missing beat in a process state (pre-journal legacy, or the lane just hasn't called write_heartbeat yet); OR a beat whose STATE doesn't match the journal but only by misconfiguration | resume allowed for **capture/inspection lanes only** — re-run the wrapper (`docs/m4-massive-runbook.md` §5); adopt the stale lease first (§2.1) |
| `UNKNOWN_RECONCILIATION_REQUIRED` | dead in the sealed lane — authority was already consumed; OR a heartbeat/state mismatch with the journal | **owner decision, nothing automatic.** A retry could double-spend the one-shot seal; write a RECONCILIATION_NOTE after the owner rules |

`FAILED` never appears from silence — only from an explicit journal
transition.

### 1.5 era_status exit table (verbatim contract)

```
Exit codes (contract; also in docs/m4-closeout-runbook.md):
  0  determinate state (ALIVE / DEAD_TERMINAL / terminal journal state)
  2  store unreadable/corrupt (journal mid-file corruption, torn projection,
     or a store whose identity names a DIFFERENT run than its directory —
     misfiled evidence, round-3 review 2026-08-23)
  3  UNKNOWN / RECONCILIATION_REQUIRED
  4  no run found (and no legacy capture process either)
```

## 2. Recovery

### 2.1 Reboot (boot-id change)

A reboot changes `/proc/sys/kernel/random/boot_id`; the lease
classifier answers `STALE_BOOT_CHANGED` — boot identity dominates pid
liveness, because a pid number from a previous boot is meaningless on
this one, alive-looking or not.

What is GONE: everything under `/tmp` — including the era log
(`/tmp/m4h-era.log`) and any marker file. What SURVIVES on disk: the
era directory `artifacts/m4b-coverage-era/` (masters, spot proxy, the
wrapper `run.sh`, and `capture_manifest.json`, which the capture
rewrites from disk on every exit path), the response cache
(`artifacts/massive-cache/`), and any runstate store
(`artifacts/runstate/<run-id>/`). Progress is durable; the /tmp log is
not — which is exactly why §4.1 copies it into the repo the moment the
era exits.

Recovery sequence (run `era_status` FIRST — it is read-only):

```
uv run --frozen python scripts/era_status.py --json            # read-only
uv run --frozen python scripts/runstate_mark.py <run-id> INSPECTION_RUNNING \
    --adopt-stale-lease \
    --reason "reboot recovery: old owner died with the previous boot"  # mutating
```

`--adopt-stale-lease` is ONLY legal against a provably stale lease
(dead pid / boot change / pid reuse / torn). A `HELD` lease is refused
outright (exit 3) — a live owner is presumed working.

For the pre-journal legacy era there is no lease to adopt: re-run the
wrapper directly (`bash artifacts/m4b-coverage-era/run.sh`); cached
pages are free and the manifest is rebuilt from disk.

### 2.2 OOM / kill / disconnect

A killed process is UNKNOWN (rule §0.1). The journal is the truth —
read it with `era_status`, then decide by lane:

| journal state | classification | decision |
|---|---|---|
| `CAPTURING` dead | `UNKNOWN_RESUMABLE` | resume: re-run the wrapper; the cache makes it free |
| `INSPECTION_RUNNING` dead | `UNKNOWN_RESUMABLE` | resume: re-run the inspector lane |
| `BARS_CAPTURING` dead | `UNKNOWN_RESUMABLE` | resume per the bars lane's own resume path |
| `SEALED_RUNNING` dead | `UNKNOWN_RECONCILIATION_REQUIRED` | owner decision only — authority was consumed; a blind retry double-spends the one-shot seal |
| anything else | per §1.4 | — |

Never mark a run FAILED because a process died. FAILED is a deliberate
journal transition with a reason, made when the lane itself concludes
failure.

### 2.3 Stale reviewer / silent-but-alive

If `era_status` reports `ALIVE_SILENT` or a `HELD` lease, the correct
action is NOTHING. The 900 s stale threshold already exceeds the
capture lane's worst quiet stretch. Killing a silent owner creates an
UNKNOWN incident where none existed; adopting its lease is refused
while it lives (exit 3). Wait, re-check, and only act when the
classification actually turns `UNKNOWN_*`.

## 3. Manifest-repair prohibition

The capture manifest (`capture_manifest.json`) is DERIVED evidence:
the capture script rewrites it from the files on disk on every exit
path. If the run's journal pins a manifest hash that differs from what
is on disk now, resume and adoption refuse with:

```
MANIFEST_MISMATCH: run <run-id>: pinned capture manifest <pinned[:12]>… but
the on-disk manifest hashes <observed[:12]>…; resume refuses. The manifest
is DERIVED evidence — re-derive it by re-running the capture (the cache
makes that free), never hand-edit it
```

Operator rules:

- A mismatch is an INCIDENT, not an input. Retain everything (§5).
- The two resolutions are (a) re-derive: re-run the capture so the
  manifest is rebuilt from the unchanged masters, or (b) owner
  reconciliation: the owner records the decision.
- Editing `capture_manifest.json`, the journal, or any pinned hash by
  hand is prohibited — it converts derived evidence into fabricated
  evidence.
- `scripts/capture_massive_structural.py` is never edited mid-era
  (rule §0.4), so the derivation rule cannot change under a live run.

## 4. Closeout sequence

Ordered. Read the exit tables in the Appendix before starting; the
expected exit for each step is stated inline. Owner gates are named
where they occur — nothing below lands on `main` without approval
(rule §0.6).

### 4.1 Era exit → copy the volatile log FIRST

When the era wrapper exits, `/tmp/m4h-era.log` is the only copy of the
run's stdout/stderr and a single reboot deletes it. The launch command
appends its own `ERA_EXIT=<code>` line (and touches
`/tmp/m4h-era.done`) when the wrapper finishes — copy BOTH facts into
the era directory IMMEDIATELY:

```
cp /tmp/m4h-era.log artifacts/m4b-coverage-era/era.log           # mutating
tail -1 artifacts/m4b-coverage-era/era.log   # read-only: the ERA_EXIT line
```

If the log has no `ERA_EXIT` line (e.g. the launcher shell itself was
killed), append one recording what is actually known — and treat the
absence as part of the incident, never guess a code:

```
printf 'ERA_EXIT=UNKNOWN (launcher shell died before recording)\n' \
    >> artifacts/m4b-coverage-era/era.log                         # mutating
```

The G4 plan's prerequisite is "coverage era COMPLETE … ERA_EXIT=0";
that line is the evidence. `ERA_EXIT` anything-but-0 stops the
sequence here and becomes an incident (§5 + owner).

### 4.2 Adopt the era into runstate

The pre-journal era gets a store now. One fact per invocation — the
CLI performs EITHER a transition OR a pin, never both (`--pin-manifest`
pins "instead of transitioning"):

```
# compute the manifest hash to pin (read-only):
sha256sum artifacts/m4b-coverage-era/capture_manifest.json

uv run --frozen python scripts/runstate_mark.py <run-id> \
    --create-identity artifacts/runstate/m4-coverage-era-identity.json \
    --reason "closeout: adopt the sealed coverage era"           # mutating
uv run --frozen python scripts/runstate_mark.py <run-id> CAPTURING \
    --reason "pre-journal legacy era; journaling the observed lane"  # mutating
uv run --frozen python scripts/runstate_mark.py <run-id> \
    --pin-manifest <capture_manifest_sha256> \
    --reason "bind the run to the sealed capture manifest"       # mutating
uv run --frozen python scripts/runstate_mark.py <run-id> CAPTURE_COMPLETE \
    --reason "wrapper ERA_EXIT=0; log copied; manifest pinned"   # mutating
```

`<run-id>` is deterministic in the run's inputs
(`tree_options.runstate.store.compute_run_id`); the identity JSON is
authored once by the operator from the era facts (campaign, protocol
hash, code sha, provider token, capture version, universe manifest
hash, boot id, pid, start time, args hash). Skips are illegal
(`PLANNED → CAPTURE_COMPLETE` exits 2): journal the lane as it
actually happened.

### 4.3 Census

```
uv run --frozen python scripts/build_coverage_census.py \
    --capture-dir artifacts/m4b-coverage-era                     # mutating
```

Defaults: `--universe data/coverage/coverage_universe.json`,
`--out-root artifacts/census`, `--calendar-dir data/calendar`. Emits
`artifacts/census/<content_sha256[:12]>/` with `census.json` (byte-
identical across re-runs over identical inputs), `census.md` (the
human summary, including the G3 derivation-source contradiction
VERBATIM), and `census.json.sha256`.

- **Exit 0 is the pass**: zero pairs in INCOMPLETE_CLASSES and masters
  observed == the universe's 3,045. Holiday semantics: spot gaps on
  holiday Fridays are EXPECTED — they are whole coverage and do NOT
  block exit 0.
- **Exit 5 means the census emitted but coverage is incomplete** — the
  artifact is still written; partial evidence is never swallowed.
  Treat exit 5 as an owner escalation, not a re-run trigger.
- **Exit 2 on a mid-run era dir is BY DESIGN** — the census consumes a
  sealed capture only (every listed file re-hashed, no unlisted
  `*.json`). Running it before the era seals is a useful dry check
  that writes nothing.
- **Exit 4 includes a dirty tracked tree** — the census is
  reproducible or refuses; commit (or clean) first. An existing
  content-addressed output directory also refuses (never overwrite).

### 4.4 Protocol 0.2.1 amendment (dry-run only, owner-gated)

```
uv run --frozen python scripts/build_protocol_amendment.py \
    --census artifacts/census/<content_sha256[:12]>/census.json \
    --owner-values /abs/path/owner-values.json \
    --rules /abs/path/ratified-rules.json \
    --capture-manifest artifacts/m4b-coverage-era/capture_manifest.json
                                                                # mutating (artifacts/ only)
```

Defaults: `--protocol research_protocol.yaml` (the base MUST be 0.2.0),
`--out-root artifacts/amendment`. The builder never chooses a value —
there is no default for `--owner-values` — and every packet it emits
says `landed: false`. Refuses (exit 2) a stale census or a capture
manifest that drifted since the census; refuses (exit 3) hidden
defaults, bool-as-int thresholds, NaN/Infinity literals,
census-binding mismatches, future-derived facts, and value ≠ rule.

**The recorded threshold-source contradiction (do not paper over).**
The G3 packet's PENDING-era checklist says the volume-flow threshold
value comes from "era bar-volume distributions"
(`docs/m4-g3-amendment-packet.md`, Ask D) — but the era ran
`--bars 0`, so no era bar-volume distributions exist. The rule per
owner decision 2026-08-23: the threshold is an OWNER-RATIFIED INPUT
bound to the census hash (supplied via `--owner-values`, bound by
`--rules`), never derived from the era, never invented by the builder.
The contradiction is recorded verbatim in the census summary; closing
it is an owner act at ratification.

Landing 0.2.1 is a separate owner-approved change after the proposal
packet is reviewed. Nothing here edits `research_protocol.yaml`.

### 4.5 Bars era (preflight now; execute doubly gated)

```
uv run --frozen python scripts/launch_bars_era.py \
    --run-id <run-id> \
    --census artifacts/census/<content_sha256[:12]>/census.json \
    --capture-manifest artifacts/m4b-coverage-era/capture_manifest.json \
    --work-manifest artifacts/bars/work-manifest.json            # read-only (preflight is the DEFAULT)
```

Omitting `--preflight` runs preflight; it is read-only. On main today
the honest answer is **exit 2** (the protocol is 0.2.0 and no
BARS_LAUNCH_APPROVAL record exists) — that is the documented correct
answer, not a failure to fix.

Execute is **doubly gated** and BOTH gates must pass: the loaded
protocol must be exactly 0.2.1 with a hash matching a
BARS_LAUNCH_APPROVAL authority record at `artifacts/bars-authority/`,
AND that record must bind the work manifest. The CLI `--execute` path
moreover refuses outright (exit 10, nothing touched): the runner is a
library-seam parameter only — authority must never be consumable from
a CLI argument. A duplicate execution refuses (exit 7); a crash after
consumption is RECONCILIATION_REQUIRED, never a retry.

`data/bars/selection-profile.json` is a DRAFT profile — its status
field says "PENDING owner ratification" and every value is tagged
`draft`. Owner ratification is required before the bars era; the
profile's content hash is bound into every work manifest, so
ratifying a change invalidates prior manifests.

### 4.6 G4 sealed-event preflight (EXECUTE IS PROHIBITED)

```
uv run --frozen python scripts/g4_seal.py preflight \
    --lane1-manifest <cboe-manifest.json> \
    --lane2-manifest artifacts/m4b-coverage-era/capture_manifest.json \
    --calendar-decision <declared-decision-not-PENDING> \
    --criteria-sha256 <sha256-of-data/g4/sealed-criteria.json> \
    --ledger-root artifacts/g4-authority                        # read-only
```

Preflight verifies the AVAILABILITY of the six sealed-run inputs
(`code_sha` from a clean tracked tree, `protocol_hash`, the lane 1 and
lane 2 manifest hashes, the holiday-calendar decision — PENDING means
undecided means unavailable — and the sealed-criteria sha256). The
output verdict is structurally null: `verdict` is pinned to `null` and
`verdict_computed` to `false`, so no code path can compute, infer, or
display one. No network, no broker, no run.

**EXECUTE IS PROHIBITED in this campaign.** `g4_seal execute` is
implemented but the CLI wires no runner and refuses (exit 2) before
touching the ledger; the one-shot consumption exists as a library seam
for the sealed event itself, after the owner declares the head. Do not
run it, do not wire a runner into it.

One-shot semantics: any CONSUMPTION record whose `sealed_run_id` OR
`content_identity` matches this run refuses (exit 7). That is why a
re-execution after a checkout change is refused — two checkouts of the
same research content share a `content_identity`, so the second
consumption is caught under EITHER id, not just the literal run id.
The CONSUMPTION record is appended durably BEFORE the runner is
invoked; a crash after consumption is UNKNOWN /
RECONCILIATION_REQUIRED and a later identical execute hits the
second-execution refusal — by design.

## 5. Incident evidence

- **Retain journal, lease, and heartbeat files verbatim.** Everything
  under `artifacts/runstate/<run-id>/` (`run.json`, `journal.jsonl`,
  `current.json`, `lease/owner.json`, `heartbeat.json`) and the ledgers
  under `artifacts/g4-authority/` / `artifacts/bars-authority/` is
  evidence. Never delete, never edit, never rebuild by hand.
- **The /tmp copy rule**: any log that lives in `/tmp`
  (`/tmp/m4h-era.log` today) must be copied into the repo's
  `artifacts/` tree IMMEDIATELY at the event it records (§4.1) — /tmp
  is volatile (rule §0.2) and it already orphaned this era once.
- An incident is recorded, then escalated to the owner. UNKNOWN states
  get a RECONCILIATION_NOTE (or the owner's decision) — never a
  silent retry, never a fabricated FAILED.

## 6. Supervisor template — DO NOT INSTALL

`templates/systemd-user/tree-options-era.service` (+ its `README.md`)
is a TEMPLATE for a future owner-authorized supervisor; installing or
enabling it is an owner decision, not a closeout step. It exists to
make the restart discipline explicit: `Restart=no` is load-bearing —
restart-after-unknown is PROHIBITED (rule §0.1). See the template's
README for the manual resume procedure it would replace.

## Appendix: CLI exit-code contracts (verbatim)

### scripts/runstate_mark.py — operator write path

```
Exit codes (contract; also in docs/m4-closeout-runbook.md):
  0  recorded
  2  illegal transition (skip/regression/UNKNOWN target)
  3  lease held by a live owner
  4  unknown run (no store; create one with --create-identity)
  5  store unreadable/corrupt (journal or projection)
  6  refused store root or run id (root resolves under /tmp, the run
     id is not a single path component under the validated root —
     absolute/parent-bearing/symlink-escaping ids are refused — or the
     opened store's identity names a DIFFERENT run than its directory:
     misfiled evidence is never aliased, round-3 review 2026-08-23)
  7  pin already bound (a DIFFERENT capture-manifest hash after a pin
     exists; the same hash re-pin is idempotent)
  8  journal concurrent write (append whose prev_record_sha256 does not
     match the locked tail, or a torn tail — repair is an owner act)
```

Usage:

```
python scripts/runstate_mark.py <run-id> CAPTURING --reason "era pass 3"
# Two-invocation form for the pin + transition (CLI refuses the
# combined form with exit 2 — see F8a round-1 review fix):
python scripts/runstate_mark.py <run-id> --pin-manifest <capture_manifest content_sha256> \
    --reason "wrapper exit 0"
python scripts/runstate_mark.py <run-id> CAPTURE_COMPLETE \
    --reason "wrapper exit 0"
python scripts/runstate_mark.py <run-id> INSPECTION_RUNNING \
    --adopt-stale-lease --reason "reboot recovery: old owner dead"
```

(One journaled fact per invocation: the CLI REFUSES to_state +
--pin-manifest in the same call with exit 2 — a combined call exits
without writing either, which is why the pin and the transition are
two separate invocations here. See `scripts/runstate_mark.py` for the
exit table. Heartbeats are written by a lane's library code
(`tree_options.runstate.store.RunStore.write_heartbeat`), not by this
CLI.)

### scripts/era_status.py — read-only status

See §1.5 for the table.

### scripts/gen_coverage_universe.py — declared-universe manifest

```
Exit codes: 0 written; 2 wrapper unreadable/unparseable; 3 grid invalid.
```

(`--from-run-sh <era wrapper>` → `--out data/coverage/coverage_universe.json`,
default; byte-identical across re-runs against the same wrapper. The
committed manifest already exists — re-running is a verification, and
a mutating one: it rewrites the file.)

### scripts/build_coverage_census.py — coverage census

```
Exit codes (contract):
  0  census emitted and coverage is whole: zero pairs in INCOMPLETE_CLASSES
     (MISSING/TRUNCATED/ERROR/SPOT_MISSING_SESSION — holiday-Friday spot gaps
     are EXPECTED and do not count) and masters observed ==
     universe.expected_masters
  2  capture manifest refused: unreadable, wrong shape, or failed
     verification against the capture directory (a MID-RUN era directory
     fails here BY DESIGN — the census consumes a sealed capture only);
     an existing-but-undecodable spot proxy refuses here too
  3  universe manifest refused: unreadable, invalid, or tampered
  4  reproducibility refusal (git unusable, tracked tree dirty, protocol or
     uv.lock unreadable, calendar fixture refused, census self-check) — or
     the content-addressed output directory already exists (never overwrite)
  5  census emitted but coverage incomplete (the artifact is STILL written;
     partial evidence is never swallowed)
```

### scripts/build_protocol_amendment.py — 0.2.1 proposal (dry-run only)

```
Exit codes (contract):
  0  built (proposal emitted; nothing landed)
  2  stale/invalid census, or the capture manifest drifted since the census
  3  owner-values/rules invalid (hidden default, bool-as-int, NaN/Infinity
     literal, census-binding mismatch, future-derived facts, value != rule)
  4  base/target protocol version violation
  5  output-root refusal (outside artifacts/)
  1  unexpected error
```

### scripts/launch_bars_era.py — bars-era preflight + doubly-gated execute

```
Exit codes (contract):
  0  preflight: every gate passed (nothing started); execute: consumed + run
  1  unexpected error
  2  protocol gate: not 0.2.1, or no BARS_LAUNCH_APPROVAL record binds the
     current protocol hash (correct on main today)
  3  census gate: census invalid, unhashable, or stale vs the capture manifest
  4  refuse-fallback: an override flag differs from its pinned constant
  5  run-state gate: store missing/unreadable, state != BARS_READY, or an
     existing lease (HELD = duplicate launch)
  6  execute: authority gates absent or mismatched (no approval record binds
     this protocol hash + work manifest, or the amendment packet hash differs)
  7  execute: duplicate — this work manifest was already consumed
  8  work-manifest gate: missing, unbound, profile mismatch, or cost mismatch
  9  vendor-key gate: key file missing or group/world readable
 10  CLI --execute refused: no runner is wired on the CLI (nothing touched)
```

### scripts/g4_seal.py — G4 preflight + one-shot execute guard

```
Exit codes:
  0  preflight: all six sealed-run inputs available (no verdict computed)
  2  bare invocation / unknown subcommand; preflight: one or more inputs
     unavailable; execute reached without internal runner wiring (refused —
     nothing is read, nothing is consumed)
  3  ledger unreadable: root refused (resolved under /tmp) or hash chain
     corrupt
  6  APPROVAL_INVALID — no approval record recomputes to this run's identity
  7  SECOND_EXECUTION_REFUSED — this sealed content was already consumed
```
