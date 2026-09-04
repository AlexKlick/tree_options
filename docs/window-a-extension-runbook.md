# Window-A extension — Phase B (grow the world) + Phase C (spend the extension window)

Owner rulings 2026-09-04: the five sealed dates window A never evaluated
(2026-07-17, 07-24, 07-31, 08-07, 08-14) become evaluable once the world
grows, as a NEW packet under a NEW authority (`--window window-a-ext-1`,
landed in PR #34). The capture runs as a **TWO-CYCLE continuation**:

- **Cycle 1 (rehearsal)** — Fridays **2026-08-28 + 2026-09-04**, launched
  after the 09-04 US close. Exercises every continuation step live (fetch,
  manifest, approval append, store walk, bars, spot-proxy-v2 rebuild) on a
  growth that nothing rides on. World grows to 09-04; three scoped dates
  mature; NOBODY registers.
- **Cycle 2 (completion)** — Fridays **2026-09-11 + 2026-09-18**, launched
  after the 09-18 US close. World grows to 09-18; all five scoped dates
  become label-complete; Phase C seals.

No vendor wire probe before the owner-approved launches (owner ruling
2026-09-04).

## The seal math (why 09-18, and why 08-21 does not matter)

A sealed date is label-complete iff it has ≥ 5 grid Fridays after it
(`label_complete_permitted_sessions`, H = `P4_GEOMETRY[0]` = 5). The grid
is **calendar Fridays inside the capture's span** (`_friday_grid`), not
the set of closes: 2026-08-21's MASTER exists (the original capture ran
through it), so 08-21 counts as a grid step even though the vendor never
carried its close. With closes through 2026-09-18 the steps after 08-14
are exactly 08-21, 08-28, 09-04, 09-11, 09-18 = 5 → **all five scoped
dates mature at world_last 2026-09-18**. Through 09-11 only, 08-14 has 4
→ the all-or-nothing registration refuses (by design; pinned by tests in
`tests/unit/test_p4_verdict.py`).

**Contingency:** if the vendor gaps a scoped Friday's close (another
08-21), extend the fetch to 09-25 BEFORE binding that cycle's approval —
after the approval, growing again needs a new manifest + approval cycle.

## Authority model (read this first)

A continuation launch **cannot** ride a green launcher preflight: the
work-manifest builder picks entries only from masters PRESENT, so a
continuation manifest must bind the GROWN capture manifest's hash — which
the frozen census (`43b0b040…` pins the coverage-era manifest
`1732e1d0…`) correctly refuses. The continuation's authority is the
**BARS_LAUNCH_APPROVAL ledger record + the journaled run store + the
wrapper**, exactly how the original era ran (it never used the execute
seam either — no `BARS_LAUNCH_CONSUMED` record exists). Per cycle, run
the preflight ONCE as documented evidence and expect **exit 3 (census
clause)** — that is the honest answer, not a failure.

Hard rules:

1. NEVER point any writer at `artifacts/m4b-coverage-era` (the census
   basis; its own `run.sh` header warns the same).
2. The new work manifest goes to a NEW path per cycle
   (`artifacts/bars/work-manifest-ext-1-c{1,2}.json`); the standing
   `work-manifest.json` is bound by the spent approval.
3. Close-check precedes approve (the contingency above).
4. In Phase C, never rebuild spot-proxy-v2 between `--approve` and
   `--execute` (the approval binds the world_id; a moved digest refuses
   at execute — by design).
5. `artifacts/` is wholly gitignored: ledger, stores, and manifests
   never touch git. The only committed surfaces are code, tests, docs,
   and (Phase C) the tracked registration + evidence.

## Per-cycle procedure (N = 1 or 2)

### Stage 0 — pre-stage (any time before the cycle's Friday)

Nothing here touches the wire. The refusal smokes (once, for the record):

```
# Smoke A — the frozen-snapshot basis refuses (documents why the launcher
# path cannot verify the live bars capture against the coverage manifest):
uv run --frozen python scripts/build_bars_work_manifest.py \
    --capture-dir artifacts/bars/capture \
    --capture-manifest artifacts/m4b-coverage-era/capture_manifest.json \
    --budget 64000 --out /tmp/ext1-smoke-a.json   # expect exit 3 (refused)

# Smoke B — no continuation work exists yet (masters end 08-21):
uv run --frozen python scripts/build_bars_work_manifest.py \
    --capture-dir artifacts/bars/capture \
    --capture-manifest artifacts/bars/capture/capture_manifest.json \
    --from-as-of 2026-08-28 --budget 8000 \
    --out /tmp/ext1-smoke-b.json                  # expect exit 3 (filter empty)
```

### Stage 1 — masters + spot (cycle Friday evening, after the US close)

```
# 1. new run store for THIS cycle (BARS_COMPLETE has no back edge):
#    identity JSON: campaign "m4-ext1-bars-cN", protocol_hash = the live
#    0.2.2 loaded hash (22c78231…), code_sha / universe_manifest_sha256
#    copied from census 43b0b040 provenance, capture_manifest_sha256 null
uv run --frozen python scripts/runstate_mark.py \
    --create-identity artifacts/runstate/m4-ext1-bars-cN-identity.json \
    --reason "ext-1 cycle N genesis (inherited evidence: coverage-era store \
m4-coverage-era-20260822-3dfe6aa1, census 43b0b040)" <run_id>
uv run --frozen python scripts/runstate_mark.py \
    --reason "ext-1 cycle N stage 1: masters+spot, Fridays <this cycle's dates>" \
    <run_id> CAPTURING

# 2. the wrapper (mirrors artifacts/bars/run.sh), detached:
#    capture_massive_structural.py --out-dir artifacts/bars/capture \
    --as-of 2026-08-28 --as-of 2026-09-04        # cycle 1 (cycle 2: 09-11, 09-18)
    --bars 0 ... (same atm-grid/dte/band/expiries/sides flags as run.sh;
    --budget 4000)
setsid ... > /tmp/m4-ext1-cN-stage1.log 2>&1   # then append ERA_EXIT to
artifacts/bars/capture/era-ext-1-cN.log
```

Post-checks: `ls artifacts/bars/capture/masters | wc -l` (cycle 1:
3103 = 3045 + 58); the capture's `spot_proxy.json` last close per name ==
the cycle's last Friday; record the new
`sha256sum artifacts/bars/capture/capture_manifest.json` as **G(N)**.
Journal `CAPTURE_COMPLETE` (reason cites G(N)).

### Stage 2 — authority (next morning)

```
# 1. the continuation work manifest (write-once) + read-only verify:
uv run --frozen python scripts/build_bars_work_manifest.py \
    --capture-dir artifacts/bars/capture \
    --capture-manifest artifacts/bars/capture/capture_manifest.json \
    --from-as-of 2026-08-28 \
    --budget 8000 --out artifacts/bars/work-manifest-ext-1-cN.json
uv run --frozen python scripts/build_bars_work_manifest.py \
    --capture-dir artifacts/bars/capture \
    --capture-manifest artifacts/bars/capture/capture_manifest.json \
    --verify artifacts/bars/work-manifest-ext-1-cN.json
# → entries ONLY for this cycle's Fridays; record the file sha256 as W(N)

# 2. journal INSPECTION_RUNNING → INSPECTED (reason cites W(N))

# 3. the OWNER act — append the approval (duplicate tuple refuses):
uv run --frozen python scripts/append_bars_launch_approval.py \
    --protocol research_protocol.yaml \
    --amendment-packet artifacts/amendment/022-declaration/5caf56568941/amendment-packet.json \
    --census artifacts/census/43b0b040ea3c/census.json \
    --work-manifest artifacts/bars/work-manifest-ext-1-cN.json \
    --reason "owner standing authorization <date>: window-A-extension continuation cycle N, Fridays <dates>, protocol 0.2.2"
# → record sha256 R(N)

# 4. journal AMENDMENT_PENDING_OWNER → AMENDMENT_READY (no-pending notes:
#    census 43b0b040 and protocol 0.2.2 stand; no threshold change), then
#    BARS_READY pinning the grown manifest (one invocation each):
uv run --frozen python scripts/runstate_mark.py \
    --reason "no amendment pending: census 43b0b040 and protocol 0.2.2 stand" \
    <run_id> AMENDMENT_PENDING_OWNER
uv run --frozen python scripts/runstate_mark.py \
    --reason "no amendment pending (carried)" <run_id> AMENDMENT_READY
uv run --frozen python scripts/runstate_mark.py \
    --pin-manifest <G(N)> \
    --reason "BARS_READY under approval <R(N)> binding work manifest <W(N)>" \
    <run_id> BARS_READY

# 5. ONCE, as documented evidence (expect exit 3 — the census clause):
uv run --frozen python scripts/launch_bars_era.py \
    --run-id <run_id> \
    --census artifacts/census/43b0b040ea3c/census.json \
    --capture-manifest artifacts/bars/capture/capture_manifest.json \
    --capture-dir artifacts/bars/capture \
    --work-manifest artifacts/bars/work-manifest-ext-1-cN.json
```

### Stage 3 — bars

Journal `BARS_CAPTURING`; run the stage-2 wrapper (same CLI with the
bars flags of `run.sh`, `--bars 2000 --budget 8000`), detached; on
ERA_EXIT=0 journal `BARS_COMPLETE` (reason cites the final manifest sha
G(N)' and the bars-file count). `scripts/era_status.py` before each
launch (no duplicate runs — there is no lease under the wrapper path).

### Stage 4 — complete the world

```
uv run --frozen python scripts/capture_spot_proxy_v2.py \
    --era-proxy artifacts/bars/capture/spot_proxy.json
# → rewrites artifacts/spot-proxy-v2.json + custody in place (atomic);
#   verify the last daily row == the cycle's last Friday, gaps []
```

Then verify: the read-only label check (cycle 1: three scoped dates
mature, 08-07/08-14 not; cycle 2: all five), and the FULL test suite on
the grown tree (proves main stays green on a grown world — the spent
window-A pins are not re-verified against live inputs).

## Phase C — the seal (after cycle 2; ~Mon 2026-09-21)

```
uv run --frozen python scripts/run_p4_holdout.py \
    --window window-a-ext-1 --register-only
# inspect docs/theory/p4-window-a-ext-1-registration.json:
#   permitted == exactly the five dates; dataset_manifest_hash == the
#   grown manifest's typed hash; world_id moved off d467d7878609
git add docs/theory/p4-window-a-ext-1-registration.json && git commit
# the OWNER act at the declared head:
uv run --frozen python scripts/run_p4_holdout.py \
    --window window-a-ext-1 --approve \
    --declared-head <sha> --reason "..."
uv run --frozen python scripts/run_p4_holdout.py \
    --window window-a-ext-1 --execute      # the ONE six-trial run
git add docs/evidence-logs/m4/m4-p4-window-a-ext-1.json && git commit
uv run --frozen python scripts/run_p4_holdout.py \
    --window window-a-ext-1 --verdict      # read-only re-read
```

PR the registration + evidence commits (owner merges NORMAL). The
extension window is then SPENT: the tracked evidence refuses any second
look from any checkout.
