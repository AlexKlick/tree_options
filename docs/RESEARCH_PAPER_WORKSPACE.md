# Research assignments and broker paper workspace

The candidate cockpit route `#/workspace` assigns bounded equity research, projects
persistent results, assigns virtual capital sleeves, records paper deployment proposals
and provides durable halt controls. The existing Research Lab worker/store and TREX
supervised execution model remain authoritative. This document describes source
behavior; deployment and external account qualification need separate receipts.

## Requested experiment capital

The operator requested 19 experiments at USD 50,000 and ten at USD 5,000:
29 sleeves totaling exactly USD 1,000,000. The allocation endpoint creates one durable
plan per paper workspace. It refuses another plan under a different request identity,
unknown sleeves, altered allocation amounts and reservations exceeding sleeve capital.
Each sleeve can bind a configured account alias; active or uncertain reservations
prevent rebinding. Different aliases can represent separately qualified provider accounts.
These virtual sleeves do not create 29 brokerage accounts or prove broker buying power.

Research uses declared modeled capital independently for each experiment. Repeating
backtests spends no brokerage capital. A sleeve ID in a research job is a provenance
label, not an economic reservation. The paper proposal checks the actual allocation
before reserving capital. The cockpit distinguishes modeled research from external
broker P&L; exact external P&L remains unavailable until fills and fees are verified.
Shared broker positions require attribution of authoritative execution facts to their
originating intents/sleeves before independent strategy economics can be claimed.

## User workflow

1. Choose a frozen dataset ID, hypothesis, supported strategy family, intended
   capital, optional experiment sleeve, candidate budget and reflection generation
   budget. GLM-5.3 reflection is optional; no Flash substitution is allowed.
2. Submit an assignment. HTTP persists an immutable spec and queued run. The
   existing ResearchWorker computes outside the request. GET returns stored state.
   Source, dependency lock, calendar and dataset bytes are bound at submission;
   changed source/data refuses reuse. Separate process and short control locks
   protect worker claims, duplicate submission, stop/resume and terminal publication.
3. Inspect train/validation/holdout provenance and published control comparison.
   Stop persists a STOP file independently of the browser or LLM. Restart reads
   durable state. An uncertain reflection call is not automatically repeated.
4. Create the 29-sleeve plan and bind sleeves to paper account aliases. Research
   remains available before account setup. Paper account views read qualification
   receipts; they do not contact a provider on GET or invent active ownership.
5. Select recorded research evidence and propose a specific strategy version and
   account/sleeve allocation. A proposal reserves virtual capital but grants no
   mandate and submits no order. General strategy proposals remain REVIEW_REQUIRED.
6. For the first operational canary, the local operator separately approves the
   exact one-share BUY limit order, capped at USD 100, one order and at most 900
   seconds TTL. The default UI proposal is 300 seconds. The local CLI/runtime performs
   fresh account recovery, account/quote/risk checks, existing mandate/permit/outbox
   handling, submit, readback and reconciliation.
7. Halt writes durable deployment and account HALT barriers without waiting for
   workspace/effect locks. An already in-flight provider request cannot be undone.
   Pending mandate revocation is reported separately; uncertain/observed effects
   retain reserved capital. Halted authority is not automatically rearmed.

## Local candidate startup

Keep the running options desk/cockpit unchanged while reviewing this candidate.
Use explicit roots; do not substitute ambient agent HOME for operator paths.

```bash
cd /home/alexk/documents/tree_options-worktrees/research-paper-workspace-20260930
uv sync --locked
export RESEARCH_WORKSPACE_DIR=/home/alexk/.local/state/trex/research-workspace
export TREX_PAPER_WORKSPACE=/home/alexk/.local/state/trex/paper-workspace
export TREX_QUANT_DATASETS_DIR=/home/alexk/.local/state/trex/quant-datasets
export TREX_PAPER_CATALOG=/home/alexk/.config/trex/paper-accounts.json
export TREX_WORKSPACE_CONTROLS=1
uv run --no-sync python -m tree_options.trex_web --host 127.0.0.1 --port 8091
```

Build static assets with `host-build npm run check` in `web/` before startup.
Source must remain clean and immutable while the worker runs. The new mutations
require actual loopback transport, loopback host and exact browser Origin;
forwarding headers confer no authority. Remote/proxied views remain inspection-only.
This is not a multi-user public authentication system. Do not expose enabled controls
publicly. Use the existing owner/gateway architecture before extending remote mutation.

For persistent installation, use the owning deployment workflow and source checkout,
with a service memory limit/admission appropriate to bounded research jobs. No new
production service was installed, switched or enabled by this source integration.
The existing single ResearchWorker dispatches comparisons, scenarios, forecasts and
quant campaigns. One long assignment can delay other queued research; it owns no broker.

## Dataset registration

`synthetic-machinery-v1` is an offline machinery demonstration. It is always explicitly
synthetic and cannot authorize trading. Server-owned additional datasets use
`TREX_QUANT_DATASETS_DIR/manifest.json`:

```json
{
  "schema": "trex-quant-datasets/1",
  "datasets": [{
    "dataset_id": "equity-panel-001",
    "label": "Frozen equity panel - qualification pending",
    "input_file": "equity-panel-001.json",
    "input_sha256": "<actual SHA-256 of frozen campaign JSON>",
    "calendar_file": "nyse-sessions.json",
    "calendar_checksum_file": "nyse-sessions.sha256",
    "data_class": "user_supplied_unqualified"
  }]
}
```

Input uses the existing `quant_campaign_io` campaign schema. Paths are relative to
this private server registry, including resolved symlink containment. HTTP accepts
IDs, not paths or raw datasets. Catalog metadata shows decision windows/counts;
held-out raw outcomes are not returned. Source campaign code/lock identity is retained
separately from the engine used for the newly assigned run.

The current evaluator models independent next-session open-to-close roundtrips.
It is not a multi-session holdings/rebalance engine. Historical listing/delisting,
corporate-action, publication/revision, price/share and universe qualification plus
persistent inventory accounting remain prerequisites for general strategy deployment.
Value, sentiment and GARCH gates stay intact. Search results are exploratory;
reading and then reusing a holdout does not produce independent confirmation.

## Paper setup and current limits

Qualify the existing IBKR paper account first using [IBKR_PAPER_SETUP.md](IBKR_PAPER_SETUP.md). Its supervised options owner remains authoritative; a read-only receipt does not establish an equity adapter.

Follow [SNAPTRADE_PAPER_SETUP.md](SNAPTRADE_PAPER_SETUP.md) for the alternate dedicated Alpaca Paper,
private binding/catalog, read-only qualification, Massive quote entitlement and the
exact local canary command. Credentials never enter browser payloads, research
specs, graph artifacts or model prompts. Massive authentication/cache/retries remain
owned by MassiveClient; canary quotes disable cache reuse and retain provider event
and SIP nanosecond timestamps. Delayed, stale or contradictory quotes fail closed.

SnapTrade SDK integration and hermetic executable canary machinery do not establish
an externally connected account, actual quote entitlement, submitted paper order or
validated fill/fee/correction source. Broker FILLED readback alone remains insufficient
for exact execution economics. General campaign deployment, automated 29-strategy
broker trading, account-level cross-strategy inventory/netting and exact per-sleeve
broker economics are not implemented by this canary phase. Live money stays disabled.

## Peer options research

[STRATEGY_PEER_INTEGRATION_20260930.md](STRATEGY_PEER_INTEGRATION_20260930.md)
reconciles the Claude team's artifacts against actual peer commits and current source.
QSL and VIXfloor source/tests are adapted into TREX; QSL shadow service templates
remain inactive. Late snapshot availability, conflicting outcomes, bootstrap sample
multiplicity, rate parsing, internal engine custody and G4 exhausted entry budgets
have explicit regression tests. Old performance claims and clock projections remain
attributed historical evidence; no corrected large-corpus rerun or promotion was done.
